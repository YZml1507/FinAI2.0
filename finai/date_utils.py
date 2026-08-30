"""date_utils.py — 日期解析工具

处理混合格式日期数据（YYYYMMDD 或 YYYY-MM-DD）。
"""

import pandas as pd


def normalize_date_column(series: pd.Series) -> pd.Series:
    """
    Normalize mixed-format dates (YYYYMMDD or YYYY-MM-DD).
    Returns datetime64[ns], unparseable → NaT.

    FINDING-143: tushare_events_dividend.parquet mixes ISO (2011-02-24, 14,782 rows)
    and compact (20210209, 20,655 rows). Never slice — use format='mixed'.

    Args:
        series: Input date column (object/string dtype)

    Returns:
        pd.Series with datetime64[ns] dtype, invalid values as NaT

    Examples:
        >>> dates = pd.Series(['20210209', '2011-02-24', None, '', '  '])
        >>> normalize_date_column(dates)
        0   2021-02-09
        1   2011-02-24
        2          NaT
        3          NaT
        4          NaT
        dtype: datetime64[ns]
    """
    # Convert to string and strip whitespace
    raw = series.astype(str).str.strip()

    # Replace empty strings with None (will become NaT)
    # Use format='mixed' to handle both YYYYMMDD and YYYY-MM-DD
    return pd.to_datetime(
        raw.where(raw.str.len() > 0),
        format="mixed",
        errors="coerce"
    )
