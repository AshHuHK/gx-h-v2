import { Context } from '@deepseek-ai/cordis'
import { createScope, scopeTarget } from '@deepseek-ai/dsh-scope'
import type { Sample } from './scores.ts'

type Evaluate = (sample: Sample) => Record<string, number> | Promise<Record<string, number>>
declare module '@deepseek-ai/cordis' {
  interface Events { 'gx/evaluate': Evaluate }
}

export async function mountEvaluator(root: Context, runId: string, attemptId: string, evaluate: Evaluate) {
  const key = { runId, attemptId }
  const scope = createScope(root, key)
  await scope.ctx.plugin((ctx: Context) => { ctx.on('gx/evaluate', evaluate) })
  const target = scopeTarget(root, key)
  const pending = new Set<Promise<unknown>>()
  let closing: Promise<void> | undefined
  return {
    async call(sample: Sample) {
      if (closing) throw new Error('scope closing')
      const result = root.serial(target, 'gx/evaluate', sample)
      pending.add(result)
      try {
        const value = await result
        if (!value) throw new Error('scoped provider unavailable')
        return value
      } finally {
        pending.delete(result)
      }
    },
    dispose() {
      return closing ??= Promise.allSettled([...pending]).then(() => scope.dispose())
    },
  }
}
