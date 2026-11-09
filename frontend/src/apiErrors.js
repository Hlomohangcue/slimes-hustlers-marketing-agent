function formatDetail(detail) {
  if (typeof detail === 'string') return detail.trim();

  if (Array.isArray(detail)) {
    return detail.map(formatDetail).filter(Boolean).join('; ');
  }

  if (detail && typeof detail === 'object') {
    const location = Array.isArray(detail.loc)
      ? detail.loc.filter((part) => typeof part === 'string' || typeof part === 'number').join('.')
      : typeof detail.loc === 'string'
        ? detail.loc
        : '';
    const message = ['msg', 'message', 'detail', 'error']
      .map((key) => detail[key])
      .find((value) => typeof value === 'string' && value.trim());

    return [location, message?.trim()].filter(Boolean).join(': ');
  }

  return '';
}

export function formatApiError(detail, status) {
  const formattedDetail = formatDetail(detail);
  return formattedDetail
    ? `Request failed (${status}): ${formattedDetail}`
    : `Request failed (${status})`;
}
