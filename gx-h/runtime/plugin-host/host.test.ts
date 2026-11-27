import assert from 'node:assert/strict'
import test from 'node:test'
import { Context } from '@deepseek-ai/cordis'
import { mountEvaluator } from './host.ts'
import { evaluate as v1 } from './score-v1.ts'
import { evaluate as v2 } from './score-v2.ts'

test('two project scopes dispatch independent evaluators, drain in-flight call, remove only their listener', async () => {
  const root = new Context()
  const sample = { observations: [{ score: -2 }, { score: 0 }], threshold: 0 }
  const gate = Promise.withResolvers<void>()
  const a = await mountEvaluator(root, 'project-A', 'attempt-1', async sample => {
    await gate.promise
    return v1(sample)
  })
  const b = await mountEvaluator(root, 'project-B', 'attempt-1', v2)
  const listeners = () => root.events.dispatch('serial', ['gx/evaluate']).length
  assert.equal(listeners(), 2)
  const call = a.call(sample)
  const draining = a.dispose()
  await assert.rejects(a.call(sample), /closing/)
  assert.equal(listeners(), 2)
  assert.equal((await b.call(sample)).min_score, -2)
  gate.resolve()
  assert.equal((await call).min_score, undefined)
  await draining
  assert.equal(listeners(), 1)
  assert.equal((await b.call(sample)).count_at_or_above, 1)
  await b.dispose()
  assert.equal(listeners(), 0)
})

test('invalid or empty observations are refused', () => {
  for (const observations of [[], [{score: Infinity}], [{score: '3'}]]) {
    assert.throws(() => v1({ observations, threshold: 0 }))
  }
})
