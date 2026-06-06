
import os
import json
import time
import sys
from datetime import datetime
import vlrdevapi as vlr

# ----------------------------------------------------------------------
# 配置区 —— 所有可调参数集中在这里，不要散落到下面的逻辑里
# ----------------------------------------------------------------------
TEAM_QUERY = "TYLOO"          # 搜索用的队名。TYL 在 VLR 上的注册名是 TYLOO
SEASON_KEYWORDS = ["2026"]    # 只保留对局名里含这些关键词的比赛 (过滤赛季)
RAW_DIR = "data/raw"
SERIES_DIR = os.path.join(RAW_DIR, "series")

REQUEST_DELAY = 2.0           # 每次请求后 sleep 的秒数。务必保留，别把 VLR 请求爆了
MAX_RETRIES = 3               # 单个请求失败后的重试次数
RETRY_BACKOFF = 5.0           # 重试前的等待秒数


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def save_json(obj, path):
    """把对象存成 JSON。vlrdevapi 的模型多是 dataclass，用 default=str 兜底。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=_to_serializable)
    print(f"  ✓ 已保存 {path}")


def _to_serializable(o):
    """把 vlrdevapi 的 dataclass / 对象转成可 JSON 化的 dict。"""
    if hasattr(o, "__dict__"):
        return vars(o)
    if hasattr(o, "_asdict"):       # namedtuple
        return o._asdict()
    return str(o)


def with_retry(fn, *args, label="", **kwargs):
    """带重试的请求包装。vlrdevapi 不稳定，单次失败不应让整个脚本崩掉。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = fn(*args, **kwargs)
            time.sleep(REQUEST_DELAY)        # 成功也限速
            return result
        except Exception as e:
            print(f"  ! {label} 第 {attempt}/{MAX_RETRIES} 次失败: {type(e).__name__}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF)
    print(f"  ✗ {label} 重试耗尽，跳过。")
    return None


# ----------------------------------------------------------------------
# STEP 0 —— 连通性 & 搜到 TYL。第一次跑务必先确认这一步通过。
# ----------------------------------------------------------------------
def step0_find_team():
    print("[STEP 0] 搜索 TYL 队伍 ID ...")
    results = with_retry(vlr.search.search_teams, TEAM_QUERY, label="search_teams")
    if not results:
        print("✗ 搜不到队伍。检查网络，或换 TEAM_QUERY 的写法。")
        sys.exit(1)

    # TODO[实测确认]: 打印 results，确认每个结果对象上"队伍 ID"和"队名"的字段名。
    #                 下面假定字段为 .id 和 .name —— 按实际返回修改。
    for r in results:
        print("   候选:", r)
    team = results[0]
    team_id = getattr(team, "id", None) or getattr(team, "team_id", None)
    print(f"   选定 team_id = {team_id}")
    return team_id


# ----------------------------------------------------------------------
# STEP 1 —— 队伍基本信息 + 名单
# ----------------------------------------------------------------------
def step1_team_info(team_id):
    print("[STEP 1] 抓取队伍信息与名单 ...")
    info = with_retry(vlr.teams.info, team_id, label="teams.info")
    roster = with_retry(vlr.teams.roster, team_id, label="teams.roster")
    save_json({"info": info, "roster": roster},
              os.path.join(RAW_DIR, "team_info.json"))


# ----------------------------------------------------------------------
# STEP 2 —— TYL 的全部已完成对局列表
# ----------------------------------------------------------------------
def step2_match_list(team_id):
    print("[STEP 2] 抓取对局列表 ...")
    matches = with_retry(vlr.teams.completed_matches, team_id, label="completed_matches")
    if not matches:
        print("✗ 拿不到对局列表。")
        sys.exit(1)

    def in_season(m):
        dt = getattr(m, "match_datetime", None)
        if isinstance(dt, datetime):
            return dt.year == 2026
        # 兜底
        name = str(getattr(m, "tournament_name", "") or "")
        return "2026" in name or " 26" in name or "'26" in name

    filtered = [m for m in matches if in_season(m)]
    print(f"   对局总数 {len(matches)},2026 赛季内 {len(filtered)} 场")
    save_json(filtered, os.path.join(RAW_DIR, "match_list.json"))
    return filtered
# ----------------------------------------------------------------------
# STEP 3 —— 逐个 BO 抓详细数据 (地图、ban/pick、逐回合)
# ----------------------------------------------------------------------
def step3_series_detail(match_list):
    print(f"[STEP 3] 抓取 {len(match_list)} 个系列赛的详细数据 ...")
    os.makedirs(SERIES_DIR, exist_ok=True)

    for i, m in enumerate(match_list, 1):
        # TODO[实测确认]: 确认对局对象里"系列赛 ID"的字段名
        match_id = getattr(m, "id", None) or getattr(m, "match_id", None)
        out_path = os.path.join(SERIES_DIR, f"{match_id}.json")

        # 断点续抓:已存在就跳过。脚本中途挂掉重跑不会重复抓。
        if os.path.exists(out_path):
            print(f"  [{i}/{len(match_list)}] {match_id} 已存在，跳过")
            continue

        print(f"  [{i}/{len(match_list)}] 抓取系列赛 {match_id} ...")
        series_info = with_retry(vlr.series.info, match_id,
                                 label=f"series.info({match_id})")
        series_maps = with_retry(vlr.series.matches, match_id,
                                 label=f"series.matches({match_id})")
        if series_info is None and series_maps is None:
            continue
        save_json({"info": series_info, "maps": series_maps}, out_path)


# ----------------------------------------------------------------------
def main():
    # 礼貌限速:也可用库自带的全局限速配置
    try:
        vlr.configure_rate_limit(requests_per_second=0.5)
    except Exception:
        pass

    team_id = step0_find_team()
    step1_team_info(team_id)
    matches = step2_match_list(team_id)
    step3_series_detail(matches)
    print("\n全部完成。原始数据在 data/raw/ ，接下来打开 analysis.ipynb。")


if __name__ == "__main__":
    main()
