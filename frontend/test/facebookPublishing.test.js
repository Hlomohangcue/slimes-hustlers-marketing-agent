import test from 'node:test'
import assert from 'node:assert/strict'
import {
  approveAndPublishFacebook,
  publishFacebook,
  requiresFacebookOutcomeVerification,
} from '../src/facebookPublishing.js'

const post = { post_id: 'PST-facebook', platform: 'facebook' }

test('Facebook approval is followed by publishing and returns the confirmed post ID', async () => {
  const calls = []
  const request = async (path, options) => {
    calls.push([path, options.method])
    if (path.endsWith('/approve')) return { status: 'APPROVED' }
    return { external_post_id: '123_456', published_at: '2026-10-28' }
  }

  const published = await approveAndPublishFacebook(post, request)

  assert.deepEqual(calls, [
    ['/posts/PST-facebook/approve', 'POST'],
    ['/posts/PST-facebook/publish/facebook', 'POST'],
  ])
  assert.equal(published.external_post_id, '123_456')
})

test('Facebook approval does not publish without authenticated approval', async () => {
  const calls = []
  const request = async (path) => {
    calls.push(path)
    throw new Error('Your session expired or was rejected.')
  }

  await assert.rejects(
    approveAndPublishFacebook(post, request),
    /session expired/,
  )
  assert.deepEqual(calls, ['/posts/PST-facebook/approve'])
})

test('Facebook publishing failure is surfaced and allows a later retry', async () => {
  let publishAttempts = 0
  const request = async (path) => {
    if (path.endsWith('/approve')) return { status: 'APPROVED' }
    publishAttempts += 1
    throw new Error('Facebook publishing is not configured.')
  }

  await assert.rejects(
    approveAndPublishFacebook(post, request),
    /not configured/,
  )
  assert.equal(publishAttempts, 1)

  const retryCalls = []
  const retryPublished = await publishFacebook(post, async (path) => {
    retryCalls.push(path)
    return { external_post_id: '123_456' }
  })
  assert.deepEqual(retryCalls, ['/posts/PST-facebook/publish/facebook'])
  assert.equal(retryPublished.external_post_id, '123_456')
})

test('duplicate publishing errors are not converted into confirmed success', async () => {
  const request = async (path) => {
    if (path.endsWith('/approve')) return { status: 'APPROVED' }
    throw new Error('This post has already been published to Facebook.')
  }

  await assert.rejects(
    approveAndPublishFacebook(post, request),
    /already been published/,
  )
})

test('uncertain publication state requires manual verification', () => {
  assert.equal(requiresFacebookOutcomeVerification({
    publication_error: 'FACEBOOK_PUBLISH_OUTCOME_UNCERTAIN',
  }), true)
  assert.equal(requiresFacebookOutcomeVerification({ publication_error: null }), false)
})

test('a successful-looking publish response without a Meta post ID requires verification', async () => {
  let publishAttempts = 0
  const request = async (path) => {
    if (path.endsWith('/approve')) return { status: 'APPROVED' }
    publishAttempts += 1
    return { status: 'ok' }
  }

  await assert.rejects(
    approveAndPublishFacebook(post, request),
    /outcome is unconfirmed.*Verify the Facebook Page before retrying/i,
  )
  assert.equal(publishAttempts, 1)
})
