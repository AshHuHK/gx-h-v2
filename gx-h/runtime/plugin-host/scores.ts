/** Summary of the observed sample only. Threshold is frozen by the GX attempt. */
export interface Observation { score: number }
export interface Sample { observations: Observation[], threshold: number }

export function summarize(sample: Sample) {
  if (!Array.isArray(sample.observations) || !sample.observations.length ||
      !Number.isFinite(sample.threshold)) throw new Error('expected observations and a finite threshold')
  let max_score = -Infinity
  let mean_score = 0
  let count_at_or_above = 0
  const count = sample.observations.length
  for (const observation of sample.observations) {
    const score = observation?.score
    if (!Number.isFinite(score)) throw new Error('expected a finite numeric score per observation')
    max_score = Math.max(max_score, score)
    mean_score += score / count
    if (score >= sample.threshold) count_at_or_above++
  }
  if (!Number.isFinite(mean_score)) throw new Error('mean exceeded numeric range')
  return { observations: count, max_score, mean_score, threshold: sample.threshold, count_at_or_above }
}
