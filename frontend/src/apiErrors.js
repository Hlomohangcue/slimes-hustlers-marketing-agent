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

export function formatApiError(detail, status, requestId) {
  const message = detail && typeof detail === 'object' && !Array.isArray(detail)
    ? formatDetail(detail.message) || formatDetail(detail)
    : formatDetail(detail);
  const fieldErrors = Array.isArray(detail?.field_errors)
    ? detail.field_errors
      .map((error) => [error.field, error.message].filter(Boolean).join(': '))
      .filter(Boolean)
      .join('; ')
    : '';
  const details = [message, fieldErrors].filter(Boolean).join(' ');
  const request = typeof requestId === 'string' && requestId.trim()
    ? ` (request ID: ${requestId.trim()})`
    : '';
  return details
    ? `Request failed (${status}): ${details}${request}`
    : `Request failed (${status})${request}`;
}
