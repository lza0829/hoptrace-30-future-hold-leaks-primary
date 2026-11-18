async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  waybills: () => req('/api/waybills'),
  timeline: (id) => req(`/api/waybills/${encodeURIComponent(id)}/timeline`),
  quarantine: () => req('/api/quarantine'),
  sidecar: () => req('/api/sidecar'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (waybillId) =>
    req(waybillId ? `/api/status-proofs?waybill_id=${encodeURIComponent(waybillId)}` : '/api/status-proofs'),
  clock: () => req('/api/clock'),
  setClock: (clockTs) =>
    req('/api/clock', { method: 'POST', body: JSON.stringify({ clock_ts: clockTs }) }),
  holds: (status) => req(status ? `/api/holds?status=${encodeURIComponent(status)}` : '/api/holds'),
  promote: (keys, clock) =>
    req('/api/holds/promote', {
      method: 'POST',
      body: JSON.stringify({
        ...(keys ? { keys } : {}),
        ...(clock
          ? { expected_clock_version: clock.version, expected_clock_ts: clock.clock_ts }
          : {}),
      }),
    }),
  holdProofs: (params = {}) => {
    const q = new URLSearchParams()
    if (params.batch_id) q.set('batch_id', params.batch_id)
    if (params.clock_version != null) q.set('clock_version', params.clock_version)
    const qs = q.toString()
    return req(`/api/hold-proofs${qs ? `?${qs}` : ''}`)
  },
  promotionBatches: (clockVersion) =>
    req(
      clockVersion != null
        ? `/api/promotion-batches?clock_version=${encodeURIComponent(clockVersion)}`
        : '/api/promotion-batches',
    ),
  clockAudit: (waybillId) =>
    req(waybillId ? `/api/clock-audit?waybill_id=${encodeURIComponent(waybillId)}` : '/api/clock-audit'),
}
