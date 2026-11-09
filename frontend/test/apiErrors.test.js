import test from 'node:test'
import assert from 'node:assert/strict'
import { formatApiError } from '../src/apiErrors.js'

test('formats string API details and preserves the HTTP status', () => {
  assert.equal(
    formatApiError('Campaign could not be created.', 422),
    'Request failed (422): Campaign could not be created.',
  )
})

test('formats object API details without JavaScript object coercion', () => {
  assert.equal(
    formatApiError({
      loc: ['body', 'name'],
      msg: 'String should have at most 120 characters',
    }, 422),
    'Request failed (422): body.name: String should have at most 120 characters',
  )
})

test('formats array validation details with each field location and message', () => {
  assert.equal(
    formatApiError([
      { loc: ['body', 'name'], msg: 'Field required' },
      { loc: ['body', 'audience'], msg: 'String should have at most 300 characters' },
    ], 422),
    'Request failed (422): body.name: Field required; body.audience: String should have at most 300 characters',
  )
})
