/**
 * A股模拟盘 · 云端定时派工器（Cloudflare Worker）
 *
 * 为什么需要它：
 *   GitHub 自带的 cron（schedule）对新建仓库实测完全不触发 —— 6 个计划点零执行，
 *   社区实测是新仓库要 5 小时才首次投递、之后每 4 小时才投一次，官方也不承诺准时。
 *   而手动触发（workflow_dispatch）实测 3/3 全部成功。
 *   所以这里做一件事：在交易时段内，替 GitHub 按扳机。
 *
 * 它不碰账本、不跑策略，只负责「派活」。真正的决策全在 GitHub 那边的
 * engine/cloud_run.py 里，跑一次就把当天从 09:30 到现在的行情完整重放一遍，
 * 所以派工晚几分钟完全不影响成交价和成交时刻。
 *
 * ── 部署后需要配置两项 ─────────────────────────────
 *   Settings → Variables and Secrets：
 *     GH_TOKEN  类型 Secret  值 = 你的 GitHub token（ghp_ 开头）
 *     REPO      类型 Text    值 = 874975861cai/ashare-sim
 *   Settings → Trigger Events → Cron Triggers：
 *     */10 * * * *
 * ──────────────────────────────────────────────────
 */

export default {
  async scheduled(event, env, ctx) {
    const now = new Date();
    // Cloudflare 的 cron 按 UTC 走，这里换算成北京时间再判断。
    const bj = new Date(now.getTime() + 8 * 3600 * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    const stamp =
      `${bj.getUTCFullYear()}-${pad(bj.getUTCMonth() + 1)}-${pad(bj.getUTCDate())} ` +
      `${pad(bj.getUTCHours())}:${pad(bj.getUTCMinutes())}`;

    const dow = bj.getUTCDay(); // 0=周日, 6=周六
    if (dow === 0 || dow === 6) {
      console.log(`[${stamp}] 周末，不派工`);
      return;
    }

    const hm = bj.getUTCHours() * 60 + bj.getUTCMinutes();
    // 09:25-11:40 / 12:55-15:40。两侧各留余量：
    // 早于开盘派工没有意义（分时还没生成），晚于收盘派工正好把尾盘那次补齐。
    const inWindow =
      (hm >= 9 * 60 + 25 && hm <= 11 * 60 + 40) ||
      (hm >= 12 * 60 + 55 && hm <= 15 * 60 + 40);

    if (!inWindow) {
      console.log(`[${stamp}] 非交易时段，不派工`);
      return;
    }

    if (!env.GH_TOKEN || !env.REPO) {
      console.log(`[${stamp}] 缺少 GH_TOKEN 或 REPO 变量，无法派工`);
      return;
    }

    const url = `https://api.github.com/repos/${env.REPO}/actions/workflows/trade.yml/dispatches`;

    let res;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: {
          // 注意：不要写成 "Bearer " + token 之外的形式，
          // GitHub 只认 Bearer 或 token 前缀。
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "ashare-sim-cron",
          "Content-Type": "application/json",
        },
        // force 必须是 false：走 cloud_run.py 自己的交易时段判断。
        // 传 true 会跳过时段判断，在非交易时间白跑。
        body: JSON.stringify({ ref: "main", inputs: { force: "false" } }),
      });
    } catch (e) {
      // 网络异常也要打出来，否则 Cloudflare 的日志里看不到失败原因
      console.log(`[${stamp}] 请求异常: ${e && e.message ? e.message : e}`);
      return;
    }

    if (res.ok) {
      // 成功是 204 No Content，没有响应体
      console.log(`[${stamp}] 已派工 (${res.status})`);
    } else {
      const text = await res.text();
      console.log(`[${stamp}] 派工失败 (${res.status}): ${text.slice(0, 300)}`);
    }
  },
};
