const document = {
  getElementById: (id: string) => {
    return {
      setAttribute: () => {},
      textContent: ""
    }
  }
};
const $ = (id: string) => document.getElementById(id);

function updateContextProgress(pct: number, tokens: number) {
  const arc = $("contextArc");
  const pctLabel = $("contextPct");
  const normalizedPct = Math.max(0, Math.min(pct, 1));
  const circumference = 69.12; // 2 * π * 11
  const offset = circumference * (1 - normalizedPct);
  arc.setAttribute("stroke-dashoffset", String(offset));
  // Color: green → yellow → red
  if (normalizedPct > 0.85) arc.setAttribute("stroke", "var(--red)");
  else if (normalizedPct > 0.6) arc.setAttribute("stroke", "var(--orange)");
  else arc.setAttribute("stroke", "var(--accent)");
  pctLabel.textContent = Math.round(normalizedPct * 100) + "%";
}

updateContextProgress(0, 0);
console.log("Success");
