import { devices, expect, test } from "@playwright/test";

test("English uploads, objective mastery and all text-only expression modules", async ({ page }) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page.getByRole("group", { name: "学习科目" }).getByRole("button", { name: "英语", exact: true }).click();
  await expect(page.getByRole("heading", { name: "英语能力地图" })).toBeVisible();
  await expect(page.getByRole("article")).toHaveCount(6);
  await expect(page.getByText("不播放、录制音频", { exact: false })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("english-workspace.png"), fullPage: true });

  await page.getByRole("button", { name: "试用英语合成样卷" }).click();
  await expect(page.getByRole("heading", { name: "英语作业报告" })).toBeVisible();
  const image = page.getByRole("img", { name: "英语作业第 1 页原图" });
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  await expect(page.getByText("AI 文字建议 · 不计分")).toBeVisible();
  await expect(page.getByText("AI 参考：").first()).toBeVisible();
  await page.getByRole("button", { name: "返回英语学习" }).click();

  await page.getByLabel("英语练习方向").selectOption("english-vocabulary");
  await page.getByRole("button", { name: "生成 4 道英语练习" }).click();
  await expect(page.getByRole("article")).toHaveCount(4);
  await expect(page.getByText("AI 参考：")).toHaveCount(0);
  const answers = ["apple", "book", "school", "friend"];
  for (let i = 0; i < answers.length; i++) {
    const item = page.getByRole("article").nth(i);
    await item.getByLabel(`英语第 ${i + 1} 题作答`).fill(answers[i]);
    await item.getByRole("button", { name: "提交英语作答" }).click();
    await expect(item.getByText("正确", { exact: true })).toBeVisible();
  }
  await page.reload();
  await expect(page.getByText("正确", { exact: true })).toHaveCount(4);
  await page.getByRole("button", { name: "返回英语学习" }).click();
  const vocabulary = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "词汇与词形", exact: true }) });
  await expect(vocabulary.getByText("100%")).toBeVisible();
  await expect(vocabulary.getByText("有效作答 4 道")).toBeVisible();

  for (const skill of ["english-grammar", "english-reading"]) {
    await page.getByLabel("英语练习方向").selectOption(skill);
    await page.getByRole("button", { name: "生成 4 道英语练习" }).click();
    await expect(page.getByRole("article")).toHaveCount(4);
    const item = page.getByRole("article").first();
    if (skill === "english-grammar") await item.getByRole("radio", { name: "A. am" }).check();
    else {
      await expect(item.getByText("Sam has two books.", { exact: false })).toBeVisible();
      await item.getByLabel("英语第 1 题作答").fill("two");
    }
    await item.getByRole("button", { name: "提交英语作答" }).click();
    await expect(item.getByText("正确", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "返回英语学习" }).click();
  }
  for (const skill of ["english-writing", "english-listening-text", "english-speaking-text"]) {
    await page.getByLabel("英语练习方向").selectOption(skill);
    await page.getByRole("button", { name: "生成 4 道英语练习" }).click();
    await expect(page.getByRole("article")).toHaveCount(4);
    const item = page.getByRole("article").first();
    if (skill === "english-listening-text") await expect(item.getByText("A: What time", { exact: false })).toBeVisible();
    await item.getByLabel("英语第 1 题作答").fill("I like English.");
    await item.getByRole("button", { name: "提交英语作答" }).click();
    await expect(item.getByText("AI 文字建议 · 不计分")).toBeVisible();
    await expect(item.getByText("作为客观作答证据记录", { exact: false })).toHaveCount(0);
    await page.getByRole("button", { name: "返回英语学习" }).click();
  }
  await expect(page.locator("audio, video")).toHaveCount(0);
  const recordButton = page.locator("#english-history").getByRole("button").first();
  await recordButton.click();
  const privateUrl = page.url();
  await page.getByLabel("演示身份").selectOption("demo-b");
  await page.getByRole("group", { name: "学习科目" }).getByRole("button", { name: "英语", exact: true }).click();
  await expect(page.getByText("还没有英语学习记录")).toBeVisible();
  await page.goto(privateUrl);
  await expect(page.getByRole("alert")).toHaveText("没有找到这条记录。重试");
  expect(errors).toEqual([]);
});

test.describe("English phone layout", () => {
  const { viewport, deviceScaleFactor, hasTouch, isMobile } = devices["Pixel 7"];
  test.use({ viewport, deviceScaleFactor, hasTouch, isMobile });
  test("keeps English upload and all six modules within the viewport", async ({ page }) => {
    await page.goto("/?view=english");
    await expect(page.getByRole("heading", { name: "英语作业与试卷" })).toBeVisible();
    await expect(page.getByRole("article")).toHaveCount(6);
    await expect(page.getByLabel("选择英语作业图片")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      await page.evaluate(() => window.innerWidth),
    );
    await page.screenshot({ path: test.info().outputPath("english-phone.png"), fullPage: true });
  });
});
