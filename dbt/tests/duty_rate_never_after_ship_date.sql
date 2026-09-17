-- The invariant the per-row join exists to hold: a shipment may never be
-- priced at a revision that took effect after it sailed. If this returns a
-- row, some order has been re-priced by a rule published after the fact —
-- which is the exact failure the effective-dating was built to prevent, and
-- the build must fail rather than report a plausible wrong landed cost.
select order_id, ship_date, rate_effective_from, hts_code
from {{ ref('landed_duty_by_order') }}
where rate_effective_from is not null
  and rate_effective_from > ship_date
