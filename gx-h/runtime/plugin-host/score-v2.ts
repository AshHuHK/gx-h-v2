import { summarize, type Sample } from './scores.ts'

export function evaluate(sample: Sample) {
  const summary = summarize(sample)
  let min_score = Infinity
  for (const { score } of sample.observations) min_score = Math.min(min_score, score)
  return { ...summary, min_score }
}
