export const FACEBOOK_PUBLISH_OUTCOME_UNCERTAIN = 'FACEBOOK_PUBLISH_OUTCOME_UNCERTAIN'

export function requiresFacebookOutcomeVerification(post) {
  return post.publication_error === FACEBOOK_PUBLISH_OUTCOME_UNCERTAIN
}

export async function publishFacebook(post, request) {
  const published = await request(`/posts/${post.post_id}/publish/facebook`, {
    method: 'POST',
  });

  if (!published.external_post_id) {
    throw new Error(
      'Facebook publishing outcome is unconfirmed. Verify the Facebook Page before retrying.',
    );
  }

  return published;
}

export async function approveAndPublishFacebook(post, request) {
  await request(`/posts/${post.post_id}/approve`, { method: 'POST' });
  return publishFacebook(post, request);
}
