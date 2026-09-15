-- A duty rate is either a usable ad valorem fraction, or explicitly flagged as
-- something that needs a quantity. A NULL rate with kind 'ad_valorem' or
-- 'free' would mean the parser silently lost a number — the failure mode that
-- produces a plausible wrong landed cost.
select hts_code, rate_kind, ad_valorem_rate
from {{ ref('duty_rate_current') }}
where (rate_kind in ('ad_valorem', 'free') and ad_valorem_rate is null)
   or (rate_kind in ('specific', 'compound', 'unparsed') and ad_valorem_rate is not null)
