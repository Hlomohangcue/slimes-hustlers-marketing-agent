export class DraftGenerationError extends Error {
  constructor(campaign, cause) {
    super('Draft generation failed for the saved campaign.', { cause });
    this.name = 'DraftGenerationError';
    this.campaign = campaign;
  }
}

export async function createCampaignWithDrafts(form, request, onCampaignCreated) {
  const campaign = await request('/campaigns', {
    method: 'POST',
    body: JSON.stringify(form),
  });

  onCampaignCreated(campaign);

  try {
    const drafts = await request(`/campaigns/${campaign.campaign_id}/drafts`, {
      method: 'POST',
    });
    return { campaign, drafts };
  } catch (error) {
    throw new DraftGenerationError(campaign, error);
  }
}

export function getDraftGenerationFailureMessage(campaign) {
  return `Campaign "${campaign.name}" (${campaign.campaign_id}) was saved, but draft generation failed. It remains in Campaigns. Do not create it again; contact your administrator to retry draft generation.`;
}
