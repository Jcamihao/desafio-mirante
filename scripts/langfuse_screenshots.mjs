// Tira screenshots do Langfuse self-hosted (lista de traces, um trace com spans, sessao da avaliacao).
// Uso: npm i puppeteer-core && CHROME=/caminho/chrome node scripts/langfuse_screenshots.mjs <traceId> [sessionId]
import puppeteer from "puppeteer-core";

const HOST = process.env.LANGFUSE_HOST ?? "http://localhost:3000";
const PROJECT = "modernizer";
const [traceId, sessionId] = process.argv.slice(2);
const out = (name) => `docs/images/${name}.png`;

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME,
  headless: true,
  args: ["--no-sandbox", "--window-size=1600,1100"],
  defaultViewport: { width: 1600, height: 1100 },
});
const page = await browser.newPage();
const settle = (ms = 4000) => new Promise((r) => setTimeout(r, ms));

await page.goto(`${HOST}/auth/sign-in`, { waitUntil: "networkidle2" });
await page.type('input[type="email"], input[name="email"]', "dev@mirante.local");
await page.type('input[type="password"]', "mirante-dev-1234");
await Promise.all([page.waitForNavigation({ waitUntil: "networkidle2" }), page.click('button[type="submit"]')]);

await page.goto(`${HOST}/project/${PROJECT}/traces`, { waitUntil: "networkidle2" });
await settle();
await page.screenshot({ path: out("langfuse-traces") });

if (traceId) {
  await page.goto(`${HOST}/project/${PROJECT}/traces/${traceId}`, { waitUntil: "networkidle2" });
  await settle(6000);
  await page.screenshot({ path: out("langfuse-trace-detail") });
}
if (sessionId) {
  await page.goto(`${HOST}/project/${PROJECT}/sessions/${sessionId}`, { waitUntil: "networkidle2" });
  await settle();
  await page.screenshot({ path: out("langfuse-session") });
}
await browser.close();
console.log("ok");
