import type { Box, Claim, DetectionStatus, Issue } from './types'

export function displayEvidence(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(displayEvidence).filter(Boolean).join('；')
  if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key}: ${displayEvidence(item)}`).join('；')
  return String(value)
}

export function claimStatus(claim: Claim): DetectionStatus {
  const value = claim.raw_status ?? claim.status
  return value && ['candidate', 'no_candidate_in_available_scope', 'unknown', 'unsupported', 'not_checked'].includes(value) ? value : 'unknown'
}

export function relatedPages(value: (number | string)[] | undefined, count: number): number[] {
  return [...new Set((value ?? []).map((item) => typeof item === 'number' ? item : /^\d+$/.test(item) ? Number(item) : NaN))]
    .filter((page) => Number.isInteger(page) && page >= 1 && page <= count).sort((a, b) => a - b)
}

// A finding counts only when the AI review did not reject it and a human did
// not mark it as a misjudgement in the evidence panel.
export function isVisible(issue: Issue): boolean {
  return issue.review_status !== 'rejected' && issue.user_verdict !== 'dismissed'
}

export function isDismissed(issue: Issue): boolean {
  return issue.user_verdict === 'dismissed'
}

// Normalized detector coordinates take precedence; legacy boxes remain in inches.
export function markerBox(issue: Issue, width?: number, height?: number): Box | null {
  let box: Box | null = null
  if (issue.normalized_bbox) {
    const value = issue.normalized_bbox
    box = Array.isArray(value) && value.length === 4
      ? { x: value[0], y: value[1], w: value[2], h: value[3] }
      : !Array.isArray(value) ? value : null
  } else if (issue.bbox && width && height) {
    box = { x: issue.bbox.x / width, y: issue.bbox.y / height, w: issue.bbox.w / width, h: issue.bbox.h / height }
  }
  if (!box || !Object.values(box).every(Number.isFinite) || box.w <= 0 || box.h <= 0) return null
  const x = Math.max(0, Math.min(1, box.x))
  const y = Math.max(0, Math.min(1, box.y))
  const right = Math.max(0, Math.min(1, box.x + box.w))
  const bottom = Math.max(0, Math.min(1, box.y + box.h))
  return right > x && bottom > y ? { x, y, w: right - x, h: bottom - y } : null
}
