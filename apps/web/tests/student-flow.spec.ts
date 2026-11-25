import { devices, expect, test } from "@playwright/test";

test("checks homework, traces mastery evidence and reflects independent practice", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "最近作业" })).toBeVisible();
  await expect(page.getByText("还没有作业记录")).toBeVisible();

  const sample = await page.request.get("/api/demo/sample", { headers: { "X-Demo-Student": "demo-a" } });
  expect(sample.ok()).toBeTruthy();
  await page.locator('input[type="file"]').setInputFiles({
    name: "合成样卷.png", mimeType: "image/png", buffer: await sample.body(),
  });
  await expect(page.getByText("合成样卷.png")).toBeVisible();
  await page.getByRole("button", { name: "开始分析" }).click();
  await expect(page.getByRole("heading", { name: "作业报告" })).toBeVisible();
  const photo = page.getByRole("img", { name: "作业第 1 页原图" });
  await expect(photo).toBeVisible();
  await expect.poll(() => photo.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
  await page.screenshot({ path: test.info().outputPath("report-desktop.png"), fullPage: true, animations: "disabled" });
  await page.getByRole("button", { name: /2\s*错误/ }).click();
  await expect(page.getByText("第 2 题 · 错误")).toBeVisible();
  await page.getByRole("button", { name: /0\s*无法判定/ }).click();
  await expect(page.getByText("这组没有题目")).toBeVisible();
  await expect(page.getByText("第 2 题 · 错误")).toHaveCount(0);

  const nav = page.getByRole("navigation", { name: "主导航" });
  const skill = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "有理数加减", exact: true }) });
  await nav.getByRole("button", { name: "知识点地图" }).click();
  await expect(skill.getByText("66%")).toBeVisible();
  await skill.getByText("查看作答依据").click();
  await skill.getByRole("listitem").filter({ hasText: "错误" }).getByRole("button").click();
  await expect(page.getByText("第 2 题 · 错误")).toBeVisible();
  const reportUrl = page.url();
  await page.getByRole("button", { name: "核对转写" }).click();
  await page.getByLabel("按原图写出你的作答").fill("8");
  await page.getByRole("button", { name: "核对结果" }).click();
  await expect(page.getByText("此题为转写核对结果，未计入掌握度。")).toBeVisible();

  await nav.getByRole("button", { name: "知识点地图" }).click();
  await expect(skill.getByText("有效作答 2 道")).toBeVisible();
  await skill.getByRole("button", { name: "做诊断练习" }).click();
  await expect(page.getByLabel("这次想练")).toHaveValue("rational-sum");
  await page.getByRole("button", { name: "生成 4 道练习" }).click();
  await expect(page.getByRole("heading", { name: "诊断练习" })).toBeVisible();
  await expect(page.getByRole("article")).toHaveCount(4);
  await expect(page.getByText(/核验结果/)).toHaveCount(0);

  const independent = page.getByRole("article").filter({ hasText: "练习 01" });
  await independent.getByLabel("第 1 题作答").fill("-5");
  await independent.getByRole("button", { name: "提交作答" }).click();
  await expect(independent.getByText("答对了")).toBeVisible();
  const hinted = page.getByRole("article").filter({ hasText: "练习 02" });
  await hinted.getByRole("button", { name: "看提示" }).click();
  await expect(hinted.getByText("本题不再计入掌握度。")).toBeVisible();
  await hinted.getByLabel("第 2 题作答").fill("-6");
  await hinted.getByRole("button", { name: "提交作答" }).click();
  await expect(hinted.getByText(/看过提示，这次作答用于练习/)).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("practice-desktop.png"), fullPage: true, animations: "disabled" });
  await page.getByRole("button", { name: "查看知识点地图", exact: true }).click();
  await expect(skill.getByText("有效作答 3 道")).toBeVisible();
  await expect(skill.getByText("100%")).toBeVisible();

  await page.getByLabel("演示身份").selectOption("demo-b");
  await expect(page.getByText("还没有作业记录")).toBeVisible();
  await page.goto(reportUrl);
  await expect(page.getByRole("alert")).toHaveText("没有找到这条记录。重试");
  await expect(photo).toHaveCount(0);
  expect(errors).toEqual([]);
});

test.describe("phone layout", () => {
  const { viewport, deviceScaleFactor, hasTouch, isMobile } = devices["Pixel 7"];
  test.use({ viewport, deviceScaleFactor, hasTouch, isMobile });

  test("shows the upload controls without horizontal overflow", async ({ page }) => {
    await page.goto("/");
    const upload = page.getByRole("link", { name: "上传作业" });
    await expect(upload).toBeVisible();
    await upload.tap();
    await expect(page.getByRole("button", { name: "选择图片" })).toBeInViewport();
    await expect(page.getByRole("button", { name: "试用合成样卷" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      await page.evaluate(() => window.innerWidth),
    );
    await page.getByRole("button", { name: "试用合成样卷" }).click();
    await expect(page.getByRole("heading", { name: "作业报告" })).toBeVisible();
    await expect.poll(() => page.getByRole("img", { name: "作业第 1 页原图" })
      .evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      await page.evaluate(() => window.innerWidth),
    );
    await page.screenshot({ path: test.info().outputPath("report-phone.png"), fullPage: true, animations: "disabled" });
    await page.getByRole("navigation", { name: "主导航" }).getByRole("button", { name: "练习台" }).click();
    await page.getByRole("button", { name: "生成 4 道练习" }).click();
    await expect(page.getByRole("article")).toHaveCount(4);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      await page.evaluate(() => window.innerWidth),
    );
    await page.screenshot({ path: test.info().outputPath("practice-phone.png"), fullPage: true, animations: "disabled" });
  });
});

test.describe("notebook layout", () => {
  for (const width of [320, 768, 1024, 1440]) {
    test(`learning pages fit a ${width}px viewport`, async ({ page }) => {
      await page.setViewportSize({ width, height: 960 });
      for (const [view, name] of [
        ["", "math"], ["mastery", "mastery"], ["practice", "practice"],
        ["english", "english"], ["knowledge", "knowledge"],
      ]) {
        await page.goto(view ? `/?view=${view}` : "/");
        await page.waitForLoadState("networkidle");
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(page.getByRole("alert")).toHaveCount(0);
        const dimensions = await page.evaluate(() => ({
          content: document.documentElement.scrollWidth, viewport: window.innerWidth,
        }));
        expect(dimensions.content, `${name} page overflows`).toBeLessThanOrEqual(dimensions.viewport);
        if (width === 320 || width === 1440) {
          await page.screenshot({ path: test.info().outputPath(`${name}-${width}.png`), fullPage: true, animations: "disabled" });
        }
      }
    });
  }

  test("respects reduced motion and keyboard access to content", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    expect(await page.locator(".hero").evaluate((hero) => getComputedStyle(hero).animationName)).toBe("none");
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: "跳到内容" });
    await expect(skip).toBeFocused();
    await expect(skip).toBeInViewport();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("main")).toBeFocused();

    await page.getByRole("group", { name: "学习科目" }).getByRole("button", { name: "英语", exact: true }).click();
    expect(await page.locator(".english-heading").evaluate((hero) => getComputedStyle(hero).animationName)).toBe("none");
    await page.getByRole("link", { name: "上传英语作业" }).click();
    await expect(page.getByLabel("选择英语作业图片")).toBeInViewport();
    const inputTop = await page.getByLabel("选择英语作业图片").evaluate((input) => input.getBoundingClientRect().top);
    const headerBottom = await page.locator(".site-header").evaluate((header) => header.getBoundingClientRect().bottom);
    expect(inputTop).toBeGreaterThanOrEqual(headerBottom);
  });
});
