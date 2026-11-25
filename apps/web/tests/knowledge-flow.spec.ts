import { devices, expect, test } from "@playwright/test";

test("teacher adds a shared concept and reference practice stays private and unscored", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page.getByRole("button", { name: "知识点目录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "共享知识点目录" })).toBeVisible();
  await page.getByLabel("知识点名称").fill("三角形内角和");
  await page.getByLabel("所属章节").fill("几何");
  await page.getByLabel("教学说明").fill("认识三角形三个内角之和。");
  await page.getByRole("button", { name: "增加知识点", exact: true }).click();
  const point = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "三角形内角和" }) });
  await expect(point).toBeVisible();
  await expect(point.getByText("AI 参考练习 · 不计入掌握度")).toBeVisible();
  await point.getByRole("button", { name: "生成参考练习" }).click();
  await expect(page.getByRole("article")).toHaveCount(4);
  await expect(page.getByText("本页提供 AI 参考", { exact: false })).toBeVisible();
  const first = page.getByRole("article").first();
  await first.getByLabel("第 1 题参考作答").fill("三个内角之和是180度。");
  await first.getByRole("button", { name: "提交并获取参考反馈" }).click();
  await expect(first.getByRole("heading", { name: "AI 参考反馈" })).toBeVisible();
  const privateUrl = page.url();
  await page.screenshot({ path: test.info().outputPath("knowledge-reference.png"), fullPage: true });
  await page.getByLabel("演示身份").selectOption("demo-b");
  await page.getByRole("button", { name: "知识点目录", exact: true }).click();
  await expect(point).toBeVisible();
  await expect(page.getByLabel("知识点名称")).toHaveCount(0);
  await page.goto(privateUrl);
  await expect(page.getByRole("alert")).toContainText("找不到这条学习记录");
  expect(errors).toEqual([]);
});

test("English upload creates a separate discovery record with private question links", async ({ page }) => {
  const sample = await page.request.get("/api/english/sample", { headers: { "X-Demo-Student": "demo-b" } });
  const uploaded = await page.request.post("/api/english/worksheets", {
    headers: { "X-Demo-Student": "demo-b" },
    multipart: { files: { name: "sample.png", mimeType: "image/png", buffer: await sample.body() } },
  });
  expect(uploaded.status()).toBe(202);
  await page.goto("/");
  await page.getByLabel("演示身份").selectOption("demo-b");
  await page.goto("/?view=knowledge&subject=english");
  await page.getByRole("button", { name: /作业知识点发现/ }).first().click();
  await expect(page.getByRole("heading", { name: "作业中的知识点" })).toBeVisible();
  await expect(page.getByRole("button", { name: "基础语法 · 去练习" })).toBeVisible();
  await expect(page.getByRole("link", { name: "查看原作业报告" })).toHaveAttribute(
    "href", `?view=english&id=${(await uploaded.json()).id}`,
  );
});

test.describe("knowledge phone layout", () => {
  const { viewport, deviceScaleFactor, hasTouch, isMobile } = devices["Pixel 7"];
  test.use({ viewport, deviceScaleFactor, hasTouch, isMobile });
  test("directory and manager form fit the viewport", async ({ page }) => {
    await page.goto("/?view=knowledge");
    await expect(page.getByRole("heading", { name: "共享知识点目录" })).toBeVisible();
    await expect(page.getByLabel("知识点名称")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      await page.evaluate(() => window.innerWidth),
    );
    await page.screenshot({ path: test.info().outputPath("knowledge-phone.png"), fullPage: true });
  });
});
