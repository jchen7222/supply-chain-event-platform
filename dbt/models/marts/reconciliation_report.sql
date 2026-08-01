-- The deliverable of the asymmetry lane: pairs ranked by UNEXPLAINED gap.
-- Asymmetry is not error — the explained/residual split is the honest output.
select pair, year, gap_usd, gap_pct,
       valuation_component_usd as explained_valuation_usd,
       residual_attribution_usd as residual_usd,
       round(100.0 * residual_attribution_usd / nullif(gap_usd, 0), 1)
           as residual_share_pct
from {{ ref('trade_asymmetry') }}
order by abs(residual_attribution_usd) desc
