export function createSessionEpoch() {
  let generation = 0
  return {
    current: () => generation,
    invalidate: () => {
      generation += 1
      return generation
    },
    isCurrent: (candidate) => candidate === generation,
  }
}

export async function resolveForSession(session, generation, request) {
  const value = await request
  return session.isCurrent(generation)
    ? { current: true, value }
    : { current: false, value: undefined }
}
