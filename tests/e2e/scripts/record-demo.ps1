<#
.SYNOPSIS
    Stage 11.5: Lumen AI M1 demo screenshot recorder (Windows).

.DESCRIPTION
    Windows counterpart to tests/e2e/scripts/record-demo.sh. Drives
    headless Chromium through the Playwright Node API to capture 11
    PNGs into the repo-root images/ directory (one per demo step).
    Reuses the same dev-server prerequisites as the e2e suite:
    Postgres + Redis + Qdrant up, alembic upgrade head, seed.py run,
    web-sdk built, the three dev servers (api:8000, web:5173,
    widget-host:8080) reachable.

.PARAMETER WithGif
    If set, also stitches one GIF per Act using ffmpeg (must be on PATH).

.EXAMPLE
    .\scripts\record-demo.ps1
    .\scripts\record-demo.ps1 -WithGif
#>
[CmdletBinding()]
param(
    [switch]$WithGif
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TestsE2EDir = Split-Path -Parent $ScriptDir
$RepoRoot = Split-Path -Parent (Split-Path -Parent $TestsE2EDir)
$ImagesDir = Join-Path $RepoRoot 'images'

if (-not (Test-Path $ImagesDir)) {
    New-Item -ItemType Directory -Force -Path $ImagesDir | Out-Null
}

function Wait-ForUrl {
    param([string]$Url, [string]$Name)
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $null = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            Write-Host "[record-demo] ${Name}: up"
            return
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "[record-demo] ${Name}: not reachable at ${Url}"
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js is required (Playwright's Node API). Install Node 20+ and re-run."
}

Wait-ForUrl 'http://localhost:8000/health/live'     'api (8000)'
Wait-ForUrl 'http://localhost:5173'                'web (5173)'
Wait-ForUrl 'http://localhost:8080/widget-host.html' 'widget-host (8080)'

# Drive Chromium via the Playwright Node API. Same flow as the bash
# version but executed through node.exe inline.
$nodeScript = @'
import { chromium } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const IMAGES_DIR = path.resolve(__dirname, '../../../../images');

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 } });
const page = await ctx.newPage();

const shoot = async (name) => {
  const file = path.join(IMAGES_DIR, `demo-${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  console.log(`[record-demo] captured ${file}`);
};

// Act 1
{
  await page.goto('http://localhost:8080/widget-host.html');
  await page.waitForTimeout(1000);
  await shoot('act1-01-widget-closed');
  await page.locator('lumen-widget').click();
  await page.waitForTimeout(800);
  await shoot('act1-02-widget-open');
  const composer = page.frameLocator('iframe').locator('textarea, input[type="text"]').first();
  await composer.fill('How do I reset my password?');
  await composer.press('Enter');
  await page.waitForTimeout(3500);
  await shoot('act1-03-widget-chat-active');
}

// Act 2
{
  await page.goto('http://localhost:5173/login');
  await page.waitForTimeout(500);
  await page.locator('input[type="email"]').fill('agent@demo.test');
  await page.locator('input[type="password"]').fill('Demo123!');
  await shoot('act2-01-login-filled');
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(/\/inbox/, { timeout: 8000 });
  await page.waitForTimeout(500);
  await shoot('act2-02-inbox');
  await page.locator('a:has-text("Open"), tr').first().click();
  await page.waitForTimeout(1000);
  await shoot('act2-03-conversation-detail');
  await page.locator('button:has-text("Suggest")').first().click().catch(() => {});
  await page.waitForTimeout(3000);
  await shoot('act2-04-ai-suggestion');
  await page.locator('textarea').last().fill('I have reset your password. Please check your inbox.');
  await page.locator('button:has-text("Send")').first().click();
  await page.waitForTimeout(1500);
  await shoot('act2-05-send-reply');
}

// Act 3
{
  await page.goto('http://localhost:5173/login');
  await page.locator('input[type="email"]').fill('admin@demo.test');
  await page.locator('input[type="password"]').fill('Demo123!');
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(/\/(inbox|kb|settings)/, { timeout: 8000 });
  await page.goto('http://localhost:5173/settings');
  await page.waitForTimeout(1000);
  await shoot('act3-01-settings');
  await page.goto('http://localhost:5173/kb');
  await page.waitForTimeout(1000);
  await shoot('act3-02-kb-list');
  const sample = path.resolve(__dirname, '../../../../docs/sample-docs/new-feature.pdf');
  await page.locator('input[type="file"]').first().setInputFiles(sample);
  await page.waitForTimeout(2500);
  await shoot('act3-03-after-upload');
}

await browser.close();
console.log('[record-demo] done');
'@

node --input-type=module -e $nodeScript

if ($WithGif) {
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        foreach ($act in @('act1','act2','act3')) {
            $out = Join-Path $ImagesDir "demo-$act.gif"
            $pattern = Join-Path $ImagesDir "demo-$act-*.png"
            ffmpeg -y -framerate 1 -pattern_type glob -i $pattern -vf "fps=1,scale=1024:-1:flags=lanczos" $out 2>$null
            Write-Host "[record-demo] stitched $out"
        }
    } else {
        Write-Host "[record-demo] -WithGif set but ffmpeg not on PATH; skipping GIF step."
    }
}

$count = (Get-ChildItem -Path $ImagesDir -Filter 'demo-*.png' | Measure-Object).Count
Write-Host "[record-demo] wrote $count PNGs to $ImagesDir/"
