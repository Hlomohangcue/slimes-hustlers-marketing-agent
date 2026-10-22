import test from 'node:test'
import assert from 'node:assert/strict'
import { validateProductionApiUrl, viteSecretVariableNames } from '../vite.config.js'
import { createSessionEpoch, resolveForSession } from '../src/sessionEpoch.js'

test('production API URL validation rejects missing, malformed, HTTP and loopback values', () => {
  for (const value of [undefined, '', 'not a URL', 'http://api.example.com',
    'https://localhost:8765', 'https://localhost.', 'https://127.0.0.2',
    'https://0.0.0.0', 'https://[::1]:8765']) {
    assert.ok(validateProductionApiUrl(value), `expected ${value} to be rejected`)
  }
})

test('session request results only update state for the session that started them', async () => {
  const session = createSessionEpoch()
  const staleGeneration = session.current()
  let resolveOldRequest
  const oldRequest = new Promise((resolve) => { resolveOldRequest = resolve })
  const applied = []
  const staleResult = resolveForSession(session, staleGeneration, oldRequest)

  session.invalidate() // logout or session change
  resolveOldRequest('old session data')
  const oldResult = await staleResult
  if (oldResult.current) applied.push(oldResult.value)
  assert.deepEqual(applied, [])

  const activeGeneration = session.current()
  const activeResult = await resolveForSession(
    session,
    activeGeneration,
    Promise.resolve('current session data'),
  )
  if (activeResult.current) applied.push(activeResult.value)
  assert.deepEqual(applied, ['current session data'])
})

test('production API URL validation accepts a public HTTPS URL', () => {
  assert.equal(validateProductionApiUrl('https://api.example.com'), null)
})

test('secret-like Vite variable names are rejected without inspecting their values', () => {
  assert.deepEqual(viteSecretVariableNames([
    'VITE_API_BASE_URL', 'VITE_MARKETING_AUTH_SECRET', 'VITE_GEMINI_API_KEY',
    'MARKETING_AUTH_SECRET',
  ]), ['VITE_MARKETING_AUTH_SECRET', 'VITE_GEMINI_API_KEY'])
})

test('session epoch invalidation makes previous async work stale', () => {
  const session = createSessionEpoch()
  const requestGeneration = session.current()
  assert.equal(session.isCurrent(requestGeneration), true)
  session.invalidate()
  assert.equal(session.isCurrent(requestGeneration), false)
})
