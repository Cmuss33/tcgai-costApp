// Cost estimates here come from /api/cost/get_model_rates/, which derives a
// real $/token rate per model from Anthropic's own billed cost report and
// usage report for the month - never a hardcoded price list, so it stays
// correct automatically if Anthropic changes prices.

// Chat/message `model` values can carry a dated snapshot suffix
// (e.g. "claude-haiku-4-5-20251001") while the rate map is keyed by
// whatever model string Anthropic's reports used, so match by longest prefix.
export function getModelRate(rates, model) {
  if (!model || !rates) return null;
  const key = Object.keys(rates)
    .filter((k) => model.startsWith(k))
    .sort((a, b) => b.length - a.length)[0];
  return key ? rates[key] : null;
}

// A single token count (input or output) priced at its own rate, or null
// when Anthropic hasn't billed this model/direction yet this month.
export function estimateTokenCost(rate, tokens, direction) {
  if (!rate || rate[direction] == null) return null;
  return tokens * rate[direction];
}

// The "input side" of a call's cost -- base input + cache writes + cache
// reads, excluding output. Split out so UIs that show input/output cost
// separately (ChatSummaryView's per-message breakdown) can price the input
// side alone without an unrelated missing output rate turning the whole
// thing null.
//
// cacheCreationTokens/cacheReadTokens (ENG-148) are real, billed tokens
// Anthropic reports separately from base input -- cache writes at a
// premium over the input rate, cache reads at a discount -- omitting them
// (as this always did before) understated the true cost of every
// cache-hit turn. Both default to 0 for historical rows and any caller
// that doesn't have this data (pre-ENG-148 messages have 0 in the DB
// already, so passing them unconditionally is safe). Same "no partial
// estimate" rule as estimateCost: null if a component with tokens > 0 has
// no real rate for it.
export function estimateInputCost(rate, tokensIn, cacheCreationTokens = 0, cacheReadTokens = 0) {
  const input = estimateTokenCost(rate, tokensIn, "input");
  if (input == null) return null;
  let total = input;

  if (cacheCreationTokens) {
    const creation = estimateTokenCost(rate, cacheCreationTokens, "cache_creation");
    if (creation == null) return null;
    total += creation;
  }
  if (cacheReadTokens) {
    const read = estimateTokenCost(rate, cacheReadTokens, "cache_read");
    if (read == null) return null;
    total += read;
  }

  return total;
}

// Full chat/message cost (input side + output). Only null when a direction
// that actually has tokens has no real rate for it -- a partial estimate
// would be misleading, so this returns null rather than silently
// under-reporting.
export function estimateCost(rate, tokensIn, tokensOut, cacheCreationTokens = 0, cacheReadTokens = 0) {
  const input = estimateInputCost(rate, tokensIn, cacheCreationTokens, cacheReadTokens);
  const output = estimateTokenCost(rate, tokensOut, "output");
  if (input == null || output == null) return null;
  return input + output;
}

export function formatCost(value) {
  return value == null ? "—" : `$${value.toPrecision(2)}`;
}
