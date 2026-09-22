"""e61 披露密度/公告频率信号构造（配合 E61_DISCLOSURE_DENSITY_PREREG）。

输入: data/notice_meta/YYYYMMDD.parquet（列: 代码/名称/公告标题/公告类型/公告日期/网址）
输出: data/notice_meta/sig_monthly.parquet — (sig_date=月末, ts_code, D1..D5)

信号定义（预登记 §二，类型名已按 2024 实测值映射）:
- D1 ann_z   : 当月公告数 vs 该股前 12 个月均值 std, z 化
- D2 neg_cnt : 60 日负面类型计数（问询/关注/警示/处罚/诉讼/异常波动/停牌/风险提示）
- D3 pos_cnt : 60 日正向类型计数（增持/分配/回购/业绩预喜标题）
- D4 abn_vol : 60 日"股票交易异常波动"计数
- D5 susp_cnt: 90 日"停牌公告"计数
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / 'data' / 'notice_meta'

# 负面类型: 收到/发送类函件与负面事件标记；排除"回复/延期回复"与
# IPO/再融资/重组问询（属发行流程非监管负面）
NEG_EXACT = {
    '收到问询函公告', '问询函', '问询函其他公告', '关注函', '收到关注函公告',
    '上交所股票监管关注', '警示函公告', '实施退市风险警示', '处罚',
    '诉讼仲裁', '股票交易异常波动', '停牌公告', '终止上市风险提示',
    '风险提示性公告', '其它风险提示公告',
}
POS_EXACT = {
    '股东/实际控制人股份增持', '分配预案', '分配方案实施',
    '分配方案决议公告', '回购预案', '回购报告书', '回购实施公告',
}
POS_TITLE_TYPES = {'业绩预告', '业绩快报'}
POS_TITLE_KW = ('预增', '预盈', '略增', '扭亏', '续盈')
ABN_TYPE = '股票交易异常波动'
SUSP_TYPE = '停牌公告'


def norm_code(raw: str) -> str | None:
    """东财裸码 -> ts_code；非上市辅导码(A 开头)返回 None。"""
    c = str(raw).strip()
    if len(c) != 6 or not c[0].isdigit():
        return None
    if c[0] == '6':
        return c + '.SH'
    if c[0] in '03':
        return c + '.SZ'
    return c + '.BJ'


def load_all() -> pd.DataFrame:
    fs = sorted(SRC.glob('20*.parquet'))
    df = pd.concat(
        (pd.read_parquet(f, columns=['代码', '公告标题', '公告类型'])
         .assign(file_date=pd.to_datetime(f.stem)) for f in fs),
        ignore_index=True)
    df['ts_code'] = df['代码'].map(norm_code)
    df = df[df['ts_code'].notna()].drop(columns=['代码'])
    df['typ'] = df['公告类型']
    df['neg'] = df['typ'].isin(NEG_EXACT)
    df['pos'] = (df['typ'].isin(POS_EXACT)
                 | (df['typ'].isin(POS_TITLE_TYPES)
                    & df['公告标题'].str.contains(
                        '|'.join(POS_TITLE_KW), na=False)))
    df['abn'] = df['typ'].eq(ABN_TYPE)
    df['susp'] = df['typ'].eq(SUSP_TYPE)
    return df[['ts_code', 'file_date', 'neg', 'pos', 'abn', 'susp']]


def build(df: pd.DataFrame) -> pd.DataFrame:
    """逐股日历化后滚动计数 + 月度 ann_z, 月末截面快照。"""
    df = df.sort_values(['ts_code', 'file_date'])
    g = df.groupby(['ts_code', 'file_date'])[['neg', 'pos', 'abn', 'susp']].sum()
    g['tot'] = 1.0
    g = g.groupby(level=0).sum()  # per (code,date) already sums flags; tot=count
    # 上一步 groupby.sum 把 tot 也算成公告数 — 直接行数即公告数
    cnt = df.groupby(['ts_code', 'file_date']).size().rename('tot')
    g = g.drop(columns=['tot']).join(cnt)
    outs = []
    for code, sub in g.groupby(level=0):
        sub = sub.droplevel(0).sort_index()
        # 连续日历轴（该股首末日之间全部日历日, 缺日=0）
        idx = pd.date_range(sub.index.min(), sub.index.max(), freq='D')
        sub = sub.reindex(idx).fillna(0)
        roll60 = sub[['neg', 'pos', 'abn']].rolling(60, min_periods=20).sum()
        susp90 = sub['susp'].rolling(90, min_periods=30).sum()
        monthly_n = sub['tot'].resample('ME').sum()
        m_mean = monthly_n.rolling(12, min_periods=6).mean().shift(1)
        m_std = monthly_n.rolling(12, min_periods=6).std().shift(1)
        ann_z = (monthly_n - m_mean) / m_std.replace(0, np.nan)
        snap = pd.DataFrame({
            'D1': ann_z,
            'D2': roll60['neg'].resample('ME').last(),
            'D3': roll60['pos'].resample('ME').last(),
            'D4': roll60['abn'].resample('ME').last(),
            'D5': susp90.resample('ME').last(),
        })
        snap['ts_code'] = code
        outs.append(snap.reset_index(names='sig_date'))
    return pd.concat(outs, ignore_index=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(SRC / 'sig_monthly.parquet'))
    args = ap.parse_args()
    df = load_all()
    print(f'[e61] rows={len(df)} codes={df.ts_code.nunique()} '
          f'{df.file_date.min().date()}~{df.file_date.max().date()}')
    sig = build(df)
    # 逐月 winsorize 1/99
    for c in ['D1', 'D2', 'D3', 'D4', 'D5']:
        q = sig.groupby('sig_date')[c].quantile([0.01, 0.99]).unstack()
        lo = sig['sig_date'].map(q[0.01])
        hi = sig['sig_date'].map(q[0.99])
        sig[c] = sig[c].clip(lo, hi)
    sig.to_parquet(args.out)
    print(f'[e61] -> {args.out} rows={len(sig)} months={sig.sig_date.nunique()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
