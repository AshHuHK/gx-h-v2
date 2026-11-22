import { readFile } from 'node:fs/promises'
import { Context } from '@deepseek-ai/cordis'
import { mountEvaluator } from './host.ts'

const root = new URL('.', import.meta.url)
const lock = JSON.parse(await readFile(new URL('package-lock.json', root), 'utf8'))
for (const [path, spec] of Object.entries(lock.packages)) {
  if (!path) continue
  const manifest = JSON.parse(await readFile(new URL(path + '/package.json', root), 'utf8'))
  if (manifest.version !== (spec as { version: string }).version) throw new Error('restore locked package ' + path)
}

const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const request = JSON.parse(Buffer.concat(chunks).toString('utf8'))
if (!['1', '2'].includes(request.version)) throw new Error('unknown score plugin version')
const { evaluate } = await import(`./score-v${request.version}.ts`)
const provider = await mountEvaluator(new Context(), request.run_id, request.attempt_id, evaluate)
try {
  const summary = await provider.call(request.sample)
  process.stdout.write(JSON.stringify(summary))
} finally {
  await provider.dispose()
}
