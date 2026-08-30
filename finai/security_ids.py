"""Canonical security-code parsing, exchange resolution, and storage aliases.

Why this module is the ONLY place allowed to infer an exchange from digits
------------------------------------------------------------------------
A-share identifiers live in three mutually incompatible string formats, and
different stores legitimately use different ones:

======================  ==================================  =========================
format                  example                             who stores it
======================  ==================================  =========================
PREFIX_LOWER (canonical) ``sh.600519``                      baostock route, index ids
SUFFIX_UPPER (storage)   ``600519.SH``                      cold Parquet, ``kline_daily``
BARE                     ``600519``                         ``stocks.code``, ``quotes``
======================  ==================================  =========================

A format mismatch makes a SQL/Parquet filter return **zero rows**, and every
caller in this codebase reads zero rows as "this security has no data". So the
failure mode is silent and looks like missing data rather than a bug. That single
root cause produced FINDING-11, -12, -13, -20, -21 and -22 in one week.

The fix is not "one format everywhere" — forcing ``kline_daily`` readers onto the
``sh.`` form is precisely what caused FINDING-21. The fix is that every format is
DERIVED here, from one exchange-resolution rule, instead of re-guessed locally.

The 9-prefix trap
-----------------
``920xxx`` is a Beijing (北交所) listing but ``900xxx`` is a Shanghai B-share.
A naive ``startswith("9") -> sh`` rule silently drops the whole Beijing exchange
(measured: 0 of 331 BJ securities reached daily OHLCV — BUG-20260729-BJDAILY);
the over-correction ``startswith("9") -> bj`` silently misroutes Shanghai B.
Symmetrically, ``4xx``/``8xx`` are legacy Beijing segments, and three call sites
mapped them to ``sz`` — ``kline_daily`` does hold ``832317.BJ``/``833874.BJ``/
``833994.BJ``, so that branch was live and wrong.

Therefore this module enumerates only prefixes whose exchange is unambiguous and
**raises** :class:`AmbiguousSecurityCodeError` for everything else. A run that
stops and says why beats a fabricated exchange written into an authoritative
dataset. Callers that hold authoritative exchange information (e.g. the
``stocks.market`` column) pass it via ``market_name`` rather than re-deriving it.

Indices need a SEPARATE rule, and the caller must say which one applies
--------------------------------------------------------------------------
An index code and an equity code can be the same six digits on *different*
exchanges, so no amount of digit analysis can tell them apart. Measured against
this project's correctly-suffixed authoritative sources (``tushare_index_daily``,
``tushare_index_weight``, ``tushare_exports/tushare_index_index_daily.parquet``,
``daily_qfq_v1/increments/index_daily_*.parquet``) — 10 distinct index codes, of
which **6 collide with a real equity in ``stocks``**:

=============  ===================  ==========================================
index code     index is             the same six digits as an equity are
=============  ===================  ==========================================
``000001.SH``  上证综指             ``000001.SZ`` 平安银行
``000016.SH``  上证50               ``000016.SZ`` \*ST康佳A
``000300.SH``  沪深300              (``stocks`` holds a polluted ``000300`` row)
``000852.SH``  中证1000             ``000852.SZ`` 石化机械
``000905.SH``  中证500              ``000905.SZ`` 厦门港务
``000985.SH``  中证全指             ``000985.SZ`` 大庆华科
``399001.SZ``  深证成指             — no equity uses ``399``
``399005.SZ``  中小100              —
``399006.SZ``  创业板指             —
``399330.SZ``  深证300              —
=============  ===================  ==========================================

So the six ``000xxx`` indices sit on Shanghai while the six equities sharing
their digits sit on Shenzhen: the SAME bare string resolves to a DIFFERENT
exchange depending on which kind of security is meant. There is therefore
deliberately **no auto-detection** — a bare six-digit string is treated as an
equity, and a caller that means the index must say so with
``asset_type="index"``. Auto-detecting would have to break one of the two, and
silently reinterpreting ``000001`` as 上证综指 would corrupt every Ping An Bank
read in the project.

Within the index namespace the digits *are* resolvable, and only the two
segments actually observed above are enumerated (``000`` -> Shanghai, ``399`` ->
Shenzhen). Everything else raises — including Beijing's ``899xxx`` series, for
which this project holds no data at all (searched: ``stocks``,
``tushare_index_daily``, ``tushare_index_weight``, ``index_5min_v1``; zero rows),
so there is nothing to verify a rule against.

The index path additionally rejects a code whose explicit exchange CONTRADICTS
its index segment (e.g. ``000300.SZ``), because that string is a known real
corruption rather than a hypothetical: ``index_5min_v1`` stores all 530 of its
indices with a ``.SZ`` suffix, 沪深300 included (FINDING-58-NEW-2).
"""
from __future__ import annotations


MARKETS: tuple[str, ...] = ("sh", "sz", "bj")
_MARKETS = frozenset(MARKETS)

#: What kind of security a code denotes. Never inferred — see the module
#: docstring: index and equity namespaces overlap, so only the caller knows.
ASSET_TYPES: tuple[str, ...] = ("equity", "index")

#: Human/DB exchange labels that authoritatively resolve the ambiguity.
_MARKET_NAMES: dict[str, str] = {
    "sh": "sh", "sz": "sz", "bj": "bj",
    "sse": "sh", "szse": "sz", "bse": "bj",
    "上交所": "sh", "上海": "sh", "上海证券交易所": "sh", "沪市": "sh",
    "深交所": "sz", "深圳": "sz", "深圳证券交易所": "sz", "深市": "sz",
    "北交所": "bj", "北京": "bj", "北京证券交易所": "bj",
}

#: Longest-prefix-first. ONLY unambiguous segments belong here; anything absent
#: raises rather than being guessed. Order matters: ``900`` must precede ``92``
#: reasoning about a leading ``9``, and both must precede any broad ``9`` rule
#: (there is deliberately no broad ``9`` rule).
_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    ("900", "sh"),   # Shanghai B-share — also starts with '9', NOT Beijing
    ("920", "bj"),   # Beijing main segment
    ("399", "sz"),   # Shenzhen index series
    ("2", "sz"),     # 200xxx Shenzhen B-share
    ("0", "sz"),
    ("3", "sz"),
    ("4", "bj"),     # legacy Beijing (NEEQ select tier transfers)
    ("6", "sh"),
    ("8", "bj"),     # legacy Beijing, e.g. 832317.BJ in kline_daily
)

#: Index segments, measured from the correctly-suffixed authoritative sources
#: listed in the module docstring. Deliberately NOT merged with
#: ``_PREFIX_RULES``: within the equity namespace ``000xxx`` is Shenzhen, within
#: the index namespace the same digits are Shanghai. That contradiction is the
#: whole reason ``asset_type`` has to be supplied by the caller.
#:
#: Beijing (``899xxx``, 北证50) is absent on purpose: this project holds zero rows
#: for it, so a rule for it could not be verified against anything.
_INDEX_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    ("000", "sh"),   # 上证/中证 series: 000001 000016 000300 000852 000905 000985
    ("399", "sz"),   # 深证/国证 series: 399001 399005 399006 399330
)


class SecurityCodeError(ValueError):
    """Base error for security-identifier resolution."""


class AmbiguousSecurityCodeError(SecurityCodeError):
    """The exchange cannot be resolved without guessing — fail closed."""


def _split_security_code(code: str) -> tuple[str | None, str]:
    """Return ``(market, bare)`` for prefix, suffix, or bare identifiers.

    ``market`` is ``None`` when the input carries no explicit exchange; NO digit
    inference happens here. ``_`` is accepted as a separator because
    ``phase2/rule_engine.py`` has always tolerated ``SH_600519``.
    """
    value = str(code).strip().replace("_", ".")
    if "." in value:
        left, right = value.split(".", 1)
        left_market = left.casefold()
        right_market = right.casefold()
        if left_market in _MARKETS:
            return left_market, right
        if right_market in _MARKETS:
            return right_market, left
    prefix = value[:2].casefold()
    if prefix in _MARKETS:
        return prefix, value[2:]
    return None, value


def bare_security_code(code: str) -> str:
    """Return the BARE form (``600519``) — what ``stocks.code``/``quotes`` store.

    Never raises: stripping an exchange requires no inference.
    """
    return _split_security_code(code)[1]


def _market_for_index_code(code: str, explicit: str | None, bare: str) -> str:
    """Exchange for an ``asset_type="index"`` code. See the module docstring.

    Unlike the equity path this DOES cross-check an explicit exchange against the
    digits, because ``000300.SZ`` is a real corruption in this repo
    (``index_5min_v1``, FINDING-58-NEW-2) rather than a hypothetical, and silently
    trusting the suffix would propagate it.
    """
    inferred: str | None = None
    for prefix, market in _INDEX_PREFIX_RULES:
        if bare.startswith(prefix):
            inferred = market
            break

    if inferred is None:
        raise AmbiguousSecurityCodeError(
            f"cannot resolve the exchange for index {code!r}. Known index segments "
            f"are {[p for p, _ in _INDEX_PREFIX_RULES]} (000xxx -> Shanghai, "
            "399xxx -> Shenzhen), measured from this project's correctly-suffixed "
            "index sources. Beijing 899xxx is deliberately absent: the project "
            "holds zero rows for it, so no rule could be verified. Note the index "
            "segments differ from the EQUITY ones -- 000xxx equity is Shenzhen but "
            "000xxx index is Shanghai -- so passing asset_type='index' for an "
            "equity (or the reverse) silently changes the exchange."
        )
    if explicit is not None and explicit != inferred:
        raise AmbiguousSecurityCodeError(
            f"exchange conflict for index {code!r}: the code claims {explicit!r} but "
            f"the {bare[:3]}xxx index segment is {inferred!r}. This is not "
            "hypothetical -- data/cold/parquet/baidu/index_5min_v1 stores all 530 of "
            "its indices with a .SZ suffix, 000300 (沪深300, really 000300.SH) "
            "included. Refusing to propagate a fabricated exchange."
        )
    return inferred


def market_for_security_code(
    code: str,
    *,
    market_name: str | None = None,
    asset_type: str = "equity",
) -> str:
    """Return the exchange (``sh``/``sz``/``bj``) for ``code``.

    This is the single authoritative exchange rule in the project. See the module
    docstring for why no other site may re-derive it.

    Args:
        code: any of the three formats.
        market_name: authoritative exchange label, e.g. ``stocks.market``
            (``"北交所"``). Used when the digits alone are ambiguous.
        asset_type: ``"equity"`` (default) or ``"index"``. NOT inferred — the two
            namespaces overlap (``000001`` is both 平安银行 on Shenzhen and 上证综指
            on Shanghai), so only the caller knows which is meant. Defaulting to
            ``"equity"`` keeps every existing call site bit-identical; a caller
            that wants the index must say so.

    Raises:
        AmbiguousSecurityCodeError: the digits are not an unambiguous segment and
            no ``market_name`` resolved them, or ``market_name`` contradicts an
            unambiguous segment (which means one of the two inputs is wrong).
        SecurityCodeError: ``asset_type`` is not a known kind.
    """
    if asset_type not in ASSET_TYPES:
        raise SecurityCodeError(
            f"unknown asset_type {asset_type!r}; expected one of {list(ASSET_TYPES)}"
        )

    explicit, bare = _split_security_code(code)
    if asset_type == "index":
        return _market_for_index_code(code, explicit, bare)
    if explicit is not None:
        return explicit

    named: str | None = None
    if market_name:
        named = _MARKET_NAMES.get(str(market_name).strip().casefold())

    inferred: str | None = None
    for prefix, market in _PREFIX_RULES:
        if bare.startswith(prefix):
            inferred = market
            break

    if inferred is not None and named is not None and inferred != named:
        raise AmbiguousSecurityCodeError(
            f"exchange conflict for {code!r}: digits imply {inferred!r} but "
            f"market_name={market_name!r} implies {named!r}; one of the two is "
            "wrong, refusing to pick one"
        )
    resolved = inferred or named
    if resolved is None:
        raise AmbiguousSecurityCodeError(
            f"cannot resolve the exchange for {code!r} without guessing. Known "
            f"unambiguous segments are {[p for p, _ in _PREFIX_RULES]}; a leading "
            "'9' is deliberately NOT one of them (920xxx is Beijing, 900xxx is "
            "Shanghai-B). Pass market_name= from an authoritative source, or a "
            "code that already carries its exchange. If you meant an INDEX, pass "
            "asset_type='index' -- indices use a different segment map."
        )
    return resolved


def canonical_security_code(
    code: str, *, market_name: str | None = None, asset_type: str = "equity"
) -> str:
    """Return the PREFIX_LOWER canonical form (``sh.600519``).

    ``asset_type="index"`` switches to the index segment map, so ``000300``
    becomes ``sh.000300`` rather than the equity reading ``sz.000300``.
    """
    market = market_for_security_code(
        code, market_name=market_name, asset_type=asset_type
    )
    return f"{market}.{bare_security_code(code)}"


def suffixed_security_code(
    code: str, *, market_name: str | None = None, asset_type: str = "equity"
) -> str:
    """Return the SUFFIX_UPPER storage form (``600519.SH``).

    This is what the cold Parquet datasets and ``phase1/finai.db::kline_daily``
    actually store, so readers of those stores want THIS function, not
    :func:`canonical_security_code` (that mix-up was FINDING-21).

    ``asset_type="index"`` yields ``000300.SH`` — the form the authoritative index
    datasets really use.
    """
    market = market_for_security_code(
        code, market_name=market_name, asset_type=asset_type
    )
    return f"{bare_security_code(code)}.{market.upper()}"


def security_code_aliases(
    code: str, *, market_name: str | None = None, asset_type: str = "equity"
) -> tuple[str, ...]:
    """Return every string form historical stores have used for ``code``.

    Intended for LOOKUP (``WHERE code IN (...)``), never for writing: when the
    exchange is ambiguous this enumerates all three exchanges rather than raising,
    because widening a read cannot fabricate an exchange, whereas picking one can.

    For the same reason ``asset_type`` matters less here than in the write paths: a
    bare index code widens to every exchange, so ``sh.000300``/``000300.SH`` are
    already included. Pass ``asset_type="index"`` to narrow to the correct exchange
    when the caller does know.
    """
    raw = str(code).strip()
    bare = bare_security_code(raw)
    try:
        market = market_for_security_code(
            raw, market_name=market_name, asset_type=asset_type
        )
    except AmbiguousSecurityCodeError:
        aliases: list[str] = [raw, bare]
        for market in MARKETS:
            aliases.extend((f"{market}.{bare}", f"{bare}.{market.upper()}"))
        return tuple(dict.fromkeys(a for a in aliases if a))
    return tuple(dict.fromkeys((
        raw,
        f"{market}.{bare}",
        bare,
        f"{bare}.{market.upper()}",
    )))
