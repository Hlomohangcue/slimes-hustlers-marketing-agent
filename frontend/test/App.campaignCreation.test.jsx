import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from '../src/App.jsx'

const campaign = {
  campaign_id: 'CMP-component-123',
  name: 'Component test campaign',
  objective: 'Test campaign recovery',
}

const draft = {
  post_id: 'PST-component-123',
  campaign_id: campaign.campaign_id,
  platform: 'linkedin',
  status: 'PENDING_APPROVAL',
  generation_source: 'template',
  content: 'Draft content for the component test.',
  version: 1,
  created_at: '2026-11-08T00:00:00',
};

function jsonResponse(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
  }
}

function deferred() {
  let resolve
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

function mockApi({
  draftRequest = async () => jsonResponse([draft]),
  postsRequest = async (current, _requestNumber, draftsGenerated) =>
    jsonResponse(draftsGenerated ? [current] : []),
  draftUpdate = async ({ content }, current) => jsonResponse({
    ...current,
    content: content.trim(),
    version: current.version + 1,
  }),
} = {}) {
  const calls = []
  let campaignCreated = false
  let draftsGenerated = false
  let postsRequestCount = 0
  let savedDraft = { ...draft }

  const fetchMock = vi.fn(async (input, options = {}) => {
    const path = new URL(input).pathname
    const method = options.method || 'GET'
    calls.push({ path, method })

    if (path === '/campaigns' && method === 'POST') {
      campaignCreated = true
      return jsonResponse(campaign)
    }

    if (path === `/campaigns/${campaign.campaign_id}/drafts` && method === 'POST') {
      const response = await draftRequest()
      draftsGenerated = response.ok
      if (response.ok) [savedDraft] = await response.json()
      return response
    }

    if (path === `/posts/${draft.post_id}/draft` && method === 'PATCH') {
      const body = JSON.parse(options.body)
      const response = await draftUpdate(body, savedDraft)
      if (response.ok) savedDraft = await response.json()
      return response
    }

    if (path === '/campaigns' && method === 'GET') {
      return jsonResponse(campaignCreated ? [campaign] : [])
    }

    if (path === '/posts' && method === 'GET') {
      return postsRequest(savedDraft, ++postsRequestCount, draftsGenerated)
    }

    throw new Error(`Unexpected mocked API request: ${method} ${path}`)
  })

  vi.stubGlobal('fetch', fetchMock)
  return calls
}

async function signIn() {
  render(<App />)
  fireEvent.change(screen.getByLabelText('Access secret'), {
    target: { value: 'component-test-only' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  await screen.findByRole('heading', { name: /Good marketing starts/ })
}

function submitCampaign() {
  fireEvent.click(screen.getByRole('button', { name: 'New campaign' }))
  fireEvent.change(screen.getByLabelText('Campaign name'), {
    target: { value: campaign.name },
  })
  fireEvent.change(screen.getByLabelText('Objective'), {
    target: { value: campaign.objective },
  })
  fireEvent.change(screen.getByLabelText('Target audience'), {
    target: { value: 'Component test users' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Create & generate drafts' }))
}

function requestCount(calls, path, method) {
  return calls.filter((call) => call.path === path && call.method === method).length
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('campaign creation component recovery', () => {
  it('shows the saved campaign in Campaigns while draft generation is still pending', async () => {
    const pendingDraftRequest = deferred()
    const calls = mockApi({ draftRequest: () => pendingDraftRequest.promise })

    await signIn()
    submitCampaign()

    await waitFor(() => {
      expect(requestCount(
        calls,
        `/campaigns/${campaign.campaign_id}/drafts`,
        'POST',
      )).toBe(1)
    })
    expect(await screen.findByText(campaign.name)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Campaigns/ }))

    expect(await screen.findByRole('heading', { name: 'Your campaigns.' })).toBeTruthy()
    expect(screen.getByText(campaign.name)).toBeTruthy()
    expect(screen.getByText(campaign.campaign_id)).toBeTruthy()
    expect(requestCount(calls, '/campaigns', 'POST')).toBe(1)

    pendingDraftRequest.resolve(jsonResponse([draft]))
    await screen.findByRole('heading', { name: /Every post/ })
  })

  it('keeps the campaign visible and resets the form after draft generation fails', async () => {
    const calls = mockApi({
      draftRequest: async () => jsonResponse({ detail: 'Draft generation failed.' }, 500),
    })

    await signIn()
    submitCampaign()

    expect(await screen.findByRole('heading', { name: 'Your campaigns.' })).toBeTruthy()
    expect(screen.getByText(campaign.name)).toBeTruthy()
    expect(screen.getByText(campaign.campaign_id)).toBeTruthy()
    const failureNotice = screen.getByText(/was saved, but draft generation failed/i)
    expect(failureNotice.textContent).toContain(campaign.campaign_id)
    expect(screen.queryByRole('heading', { name: 'Create a campaign' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'New campaign' }))
    expect(screen.getByLabelText('Campaign name').value).toBe('')
    expect(requestCount(calls, '/campaigns', 'POST')).toBe(1)
    expect(requestCount(
      calls,
      `/campaigns/${campaign.campaign_id}/drafts`,
      'POST',
    )).toBe(1)
  })

  it('preserves the recovery notice and campaign after a manual refresh', async () => {
    const calls = mockApi({
      draftRequest: async () => jsonResponse({ detail: 'Draft generation failed.' }, 500),
    })

    await signIn()
    submitCampaign()

    expect(await screen.findByText(/was saved, but draft generation failed/i)).toBeTruthy()
    const campaignListRequests = requestCount(calls, '/campaigns', 'GET')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh data' }))

    await waitFor(() => {
      expect(requestCount(calls, '/campaigns', 'GET')).toBeGreaterThan(campaignListRequests)
    })
    await waitFor(() => {
      expect(screen.queryByText('Loading workspace...')).toBeNull()
    })
    expect(screen.getByText(campaign.name)).toBeTruthy()
    expect(screen.getByText(/was saved, but draft generation failed/i)).toBeTruthy()
    expect(requestCount(calls, '/campaigns', 'POST')).toBe(1)
    expect(requestCount(
      calls,
      `/campaigns/${campaign.campaign_id}/drafts`,
      'POST',
    )).toBe(1)
  })

  it('keeps the successful workflow on Drafts with the generated draft available', async () => {
    const calls = mockApi()

    await signIn()
    submitCampaign()

    expect(await screen.findByRole('heading', { name: /Every post/ })).toBeTruthy()
    expect(screen.getByText(draft.content)).toBeTruthy()
    expect(screen.getByText('Campaign created. LinkedIn and Facebook drafts are ready for review.')).toBeTruthy()
    expect(screen.queryByText(/was saved, but draft generation failed/i)).toBeNull()
    expect(requestCount(calls, '/campaigns', 'POST')).toBe(1)
    expect(requestCount(
      calls,
      `/campaigns/${campaign.campaign_id}/drafts`,
      'POST',
    )).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: /Campaigns/ }))
    expect(screen.getByText(campaign.name)).toBeTruthy()
    expect(screen.getByText(campaign.campaign_id)).toBeTruthy()
  })

  it('does not repeat campaign creation or draft generation after failure, rerenders, or refresh', async () => {
    const calls = mockApi({
      draftRequest: async () => jsonResponse({ detail: 'Draft generation failed.' }, 500),
    })

    await signIn()
    submitCampaign()
    expect(await screen.findByText(/was saved, but draft generation failed/i)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /Overview/ }))
    fireEvent.click(screen.getByRole('button', { name: /Campaigns/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh data' }))

    await waitFor(() => {
      expect(screen.getByText(campaign.name)).toBeTruthy()
    })
    expect(requestCount(calls, '/campaigns', 'POST')).toBe(1)
    expect(requestCount(
      calls,
      `/campaigns/${campaign.campaign_id}/drafts`,
      'POST',
    )).toBe(1)
  })

  it('opens an edit, saves the pending draft, and displays the persisted version', async () => {
    const calls = mockApi()

    await signIn()
    submitCampaign()
    await screen.findByText(draft.content)
    fireEvent.click(screen.getByRole('button', { name: 'Edit draft' }))

    const editor = screen.getByRole('textbox', { name: 'Edit draft content' })
    fireEvent.change(editor, { target: { value: 'Saved edit for review.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))

    expect(await screen.findByText('Saved edit for review.')).toBeTruthy()
    expect(screen.getByText('Version 2')).toBeTruthy()
    expect(screen.getByText('Draft saved. It remains pending approval.')).toBeTruthy()
    expect(calls.filter((call) => call.method === 'PATCH')).toHaveLength(1)
  })

  it('preserves unsaved text and shows an actionable conflict on HTTP 409', async () => {
    mockApi({
      draftUpdate: async () => jsonResponse({
        detail: 'This draft changed after you loaded it. Refresh to load the latest version.',
      }, 409),
    })

    await signIn()
    submitCampaign()
    await screen.findByText(draft.content)
    fireEvent.click(screen.getByRole('button', { name: 'Edit draft' }))
    const editor = screen.getByRole('textbox', { name: 'Edit draft content' })
    fireEvent.change(editor, { target: { value: 'Keep my unsaved changes.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))

    expect((await screen.findByRole('alert')).textContent)
      .toMatch(/changed after you loaded it/i)
    expect(screen.getByRole('textbox', { name: 'Edit draft content' }).value)
      .toBe('Keep my unsaved changes.')
    expect(screen.getByText('Version 1')).toBeTruthy()
  })

  it('shows local validation errors without sending an empty edit', async () => {
    const calls = mockApi()

    await signIn()
    submitCampaign()
    await screen.findByText(draft.content)
    fireEvent.click(screen.getByRole('button', { name: 'Edit draft' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Edit draft content' }), {
      target: { value: '   ' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))

    expect((await screen.findByRole('alert')).textContent)
      .toContain('Draft content cannot be empty.')
    expect(calls.filter((call) => call.method === 'PATCH')).toHaveLength(0)
    expect(screen.getByRole('textbox', { name: 'Edit draft content' }).value).toBe('   ')
  })

  it('displays API validation errors without discarding edited text', async () => {
    mockApi({
      draftUpdate: async () => jsonResponse({
        detail: 'Draft content cannot exceed 10000 characters.',
      }, 422),
    })

    await signIn()
    submitCampaign()
    await screen.findByText(draft.content)
    fireEvent.click(screen.getByRole('button', { name: 'Edit draft' }))
    const editor = screen.getByRole('textbox', { name: 'Edit draft content' })
    fireEvent.change(editor, { target: { value: 'Text retained after validation.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))

    expect((await screen.findByRole('alert')).textContent)
      .toContain('Draft content cannot exceed 10000 characters.')
    expect(screen.getByRole('textbox', { name: 'Edit draft content' }).value)
      .toBe('Text retained after validation.')
  })

  it('keeps unsaved text and displays server errors', async () => {
    mockApi({
      draftUpdate: async () => jsonResponse({
        detail: 'Draft could not be saved.',
      }, 500),
    })

    await signIn()
    submitCampaign()
    await screen.findByText(draft.content)
    fireEvent.click(screen.getByRole('button', { name: 'Edit draft' }))
    const editor = screen.getByRole('textbox', { name: 'Edit draft content' })
    fireEvent.change(editor, { target: { value: 'Text retained after server error.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))

    expect((await screen.findByRole('alert')).textContent)
      .toContain('Draft could not be saved.')
    expect(screen.getByRole('textbox', { name: 'Edit draft content' }).value)
      .toBe('Text retained after server error.')
  })

  it('submits the version loaded for editing after refresh and preserves text on conflict', async () => {
    const pendingRefresh = deferred()
    let deferNextPostsRequest = false
    let submittedVersion
    const calls = mockApi({
      postsRequest: (current, _requestNumber, draftsGenerated) => {
        if (deferNextPostsRequest) {
          deferNextPostsRequest = false
          return pendingRefresh.promise
        }
        return jsonResponse(draftsGenerated ? [current] : [])
      },
      draftUpdate: async (body) => {
        submittedVersion = body.version
        return jsonResponse({
          detail: 'This draft changed after you loaded it. Refresh to load the latest version.',
        }, 409)
      },
    })

    await signIn()
    submitCampaign()
    await screen.findByText(draft.content)
    fireEvent.click(screen.getByRole('button', { name: 'Edit draft' }))
    const editor = screen.getByRole('textbox', { name: 'Edit draft content' })
    fireEvent.change(editor, { target: { value: 'My unsaved version one edit.' } })

    deferNextPostsRequest = true
    const previousPostsRequests = requestCount(calls, '/posts', 'GET')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh data' }))
    await waitFor(() => {
      expect(requestCount(calls, '/posts', 'GET')).toBeGreaterThan(previousPostsRequests)
    })
    pendingRefresh.resolve(jsonResponse([{
      ...draft,
      content: 'Another editor saved this.',
      version: 2,
    }]))

    expect(await screen.findByText('Latest loaded version 2')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Edit draft content' }).value)
      .toBe('My unsaved version one edit.')
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))

    expect((await screen.findByRole('alert')).textContent)
      .toMatch(/changed after you loaded it/i)
    expect(submittedVersion).toBe(1)
    expect(screen.getByRole('textbox', { name: 'Edit draft content' }).value)
      .toBe('My unsaved version one edit.')
  })

  it('reloads the latest server version only after explicit user action', async () => {
    let returnLatest = false
    const calls = mockApi({
      postsRequest: (current, _requestNumber, draftsGenerated) => jsonResponse(
        draftsGenerated
          ? [returnLatest ? {
              ...current,
              content: 'Latest server version.',
              version: 2,
            } : current]
          : [],
      ),
    })

    await signIn()
    submitCampaign()
    await screen.findByText(draft.content)
    fireEvent.click(screen.getByRole('button', { name: 'Edit draft' }))
    const editor = screen.getByRole('textbox', { name: 'Edit draft content' })
    fireEvent.change(editor, { target: { value: 'Unsaved text to keep for now.' } })
    returnLatest = true

    expect(editor.value).toBe('Unsaved text to keep for now.')
    expect(screen.getByText('Editing version 1')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Reload latest version' }))

    expect(await screen.findByDisplayValue('Latest server version.')).toBeTruthy()
    expect(screen.getByText('Editing version 2')).toBeTruthy()
    expect(screen.getByText('Version 2')).toBeTruthy()
    expect(requestCount(calls, '/posts', 'GET')).toBeGreaterThan(1)
  })

  it('does not let an older in-flight refresh overwrite a successful save', async () => {
    const staleRefresh = deferred()
    let deferNextPostsRequest = false
    const calls = mockApi({
      postsRequest: (current, _requestNumber, draftsGenerated) => {
        if (deferNextPostsRequest) {
          deferNextPostsRequest = false
          return staleRefresh.promise
        }
        return jsonResponse(draftsGenerated ? [current] : [])
      },
    })

    await signIn()
    submitCampaign()
    await screen.findByText(draft.content)
    deferNextPostsRequest = true
    const previousPostsRequests = requestCount(calls, '/posts', 'GET')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh data' }))
    await waitFor(() => {
      expect(requestCount(calls, '/posts', 'GET')).toBeGreaterThan(previousPostsRequests)
    })

    fireEvent.click(screen.getByRole('button', { name: 'Edit draft' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Edit draft content' }), {
      target: { value: 'New saved content.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))

    expect(await screen.findByText('New saved content.')).toBeTruthy()
    expect(screen.getByText('Version 2')).toBeTruthy()
    staleRefresh.resolve(jsonResponse([draft]))

    await waitFor(() => {
      expect(screen.getByText('New saved content.')).toBeTruthy()
      expect(screen.getByText('Version 2')).toBeTruthy()
    })
  })

  it('does not let an older background refresh overwrite a newer refresh', async () => {
    const olderRefresh = deferred()
    const newerRefresh = deferred()
    const pendingRefreshes = [olderRefresh, newerRefresh]
    let deferRefreshes = false
    const calls = mockApi({
      postsRequest: (current, _requestNumber, draftsGenerated) => {
        if (deferRefreshes && draftsGenerated && pendingRefreshes.length > 0) {
          return pendingRefreshes.shift().promise
        }
        return jsonResponse(draftsGenerated ? [current] : [])
      },
    })

    await signIn()
    submitCampaign()
    await screen.findByText(draft.content)

    deferRefreshes = true
    const previousPostsRequests = requestCount(calls, '/posts', 'GET')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh data' }))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh data' }))
    await waitFor(() => {
      expect(requestCount(calls, '/posts', 'GET')).toBe(previousPostsRequests + 2)
    })

    newerRefresh.resolve(jsonResponse([{
      ...draft,
      content: 'Newer refresh content.',
      version: 3,
    }]))
    expect(await screen.findByText('Newer refresh content.')).toBeTruthy()
    expect(screen.getByText('Version 3')).toBeTruthy()

    olderRefresh.resolve(jsonResponse([{
      ...draft,
      content: 'Older refresh content.',
      version: 2,
    }]))

    await waitFor(() => {
      expect(screen.getByText('Newer refresh content.')).toBeTruthy()
      expect(screen.getByText('Version 3')).toBeTruthy()
      expect(screen.queryByText('Older refresh content.')).toBeNull()
    })
  })
})
