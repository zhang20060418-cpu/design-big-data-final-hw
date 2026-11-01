import type { ReviewGate, ReviewOptions, TaskStatus } from './types'

/** null marks automatic configuration policy; a boolean is the user's choice. */
export function configuredReviewOptions(options: ReviewOptions, apiReady: boolean | null): ReviewOptions {
  const choice = options.mllm_user_choice
  if (typeof choice === 'boolean') return choice === options.enable_mllm ? options : { ...options, enable_mllm: choice }
  // Unknown legacy requests keep their saved value; backend projects unstarted
  // legacy tasks to explicit null before presenting them in Configure.
  if (choice !== null || apiReady === null || options.llm_backend === 'mock') return options
  const enabled = apiReady && options.review_mode !== 'off'
  return enabled === options.enable_mllm ? options : { ...options, enable_mllm: enabled }
}

/** Backend eligibility is authoritative; the browser only hides expired actions. */
export function canSkipReview(gate: ReviewGate | undefined | null, status: TaskStatus | undefined,
  remainingMs: number): boolean {
  if (!gate?.can_skip || !status || !['queued', 'running'].includes(status)) return false
  return gate.state === 'not_ready' || (gate.state === 'waiting' && remainingMs > 0)
}
