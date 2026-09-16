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

// Full chat/message cost only when both directions have a real rate -
// a partial estimate would be misleading, so this returns null instead.
export function estimateCost(rate, tokensIn, tokensOut) {
  const input = estimateTokenCost(rate, tokensIn, "input");
  const output = estimateTokenCost(rate, tokensOut, "output");
  if (input == null || output == null) return null;
  return input + output;
}

export function formatCost(value) {
  return value == null ? "—" : `$${value.toPrecision(2)}`;
}
