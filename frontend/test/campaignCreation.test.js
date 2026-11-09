import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createCampaignWithDrafts,
  DraftGenerationError,
  getDraftGenerationFailureMessage,
} from '../src/campaignCreation.js'

const form = {
  name: 'Pilot launch',
  objective: 'Introduce automation',
  audience: 'Small businesses',
  offer: '',
  call_to_action: 'Contact us',
}

const campaign = {
  campaign_id: 'CMP-saved-123',
  name: form.name,
}

test('creates a campaign then drafts and makes the campaign visible before drafting', async () => {
  const calls = []
  const visibleCampaigns = []
  const result = await createCampaignWithDrafts(form, async (path, options) => {
    calls.push(path)
    if (path === '/campaigns') return campaign
    assert.equal(path, `/campaigns/${campaign.campaign_id}/drafts`)
    assert.equal(options.method, 'POST')
    return [{ post_id: 'PST-1' }]
  }, (savedCampaign) => {
    visibleCampaigns.push(savedCampaign)
    assert.deepEqual(calls, ['/campaigns'])
  })

  assert.deepEqual(calls, [
    '/campaigns',
    `/campaigns/${campaign.campaign_id}/drafts`,
  ])
  assert.deepEqual(visibleCampaigns, [campaign])
  assert.equal(result.campaign.campaign_id, campaign.campaign_id)
  assert.deepEqual(result.drafts, [{ post_id: 'PST-1' }])
})

test('preserves the saved campaign on draft failure without duplicate creation or retry', async () => {
  const calls = []
  const visibleCampaigns = []
  const failure = new Error('injected draft failure')

  await assert.rejects(
    createCampaignWithDrafts(form, async (path) => {
      calls.push(path)
      if (path === '/campaigns') return campaign
      throw failure
    }, (savedCampaign) => visibleCampaigns.push(savedCampaign)),
    (error) => {
      assert.ok(error instanceof DraftGenerationError)
      assert.equal(error.campaign.campaign_id, campaign.campaign_id)
      assert.equal(error.cause, failure)
      return true
    },
  )

  assert.deepEqual(visibleCampaigns, [campaign])
  assert.equal(calls.filter((path) => path === '/campaigns').length, 1)
  assert.equal(
    calls.filter((path) => path === `/campaigns/${campaign.campaign_id}/drafts`).length,
    1,
  )
})

test('draft failure feedback says the campaign was saved and warns against recreating it', () => {
  const message = getDraftGenerationFailureMessage(campaign)

  assert.match(message, /was saved, but draft generation failed/i)
  assert.match(message, new RegExp(campaign.campaign_id))
  assert.match(message, /remains in Campaigns/i)
  assert.match(message, /Do not create it again/i)
  assert.match(message, /contact your administrator to retry draft generation/i)
})
