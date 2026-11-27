-- Historical GX charges remain immutable even when a much older store cannot
-- establish the detailed provider attribution that current turns carry.
UPDATE p0_meter_turns SET confidence='HISTORICAL_SETTLED'
WHERE attribution='HISTORICAL_TURN' AND reservation_ref IN (
  SELECT reservation_ref FROM p0_meter_native_turns
  WHERE measured_tokens IS NULL OR total_tokens IS NULL OR prior_tokens IS NULL
     OR measured_tokens<>total_tokens-prior_tokens OR terminal_status IS NULL
);
UPDATE p0_meter_threads AS t
SET reconciled_total=(SELECT max(n.total_tokens) FROM p0_meter_native_turns n
                      WHERE n.thread_id=t.thread_id AND n.confidence='PROVIDER_CONFIRMED')
WHERE observation_source='historical cumulative';
UPDATE p0_meter_threads SET confidence='PROVIDER_RECONCILED'
WHERE observation_source='historical cumulative' AND reconciled_total=observed_total;
