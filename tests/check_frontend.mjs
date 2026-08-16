// 前端 ES module 检查(CI 与本地共用)
//
// 两层检查:
//   1. 语法解析:用 vm.SourceTextModule 解析全部模块(只编译不执行),
//      语法错误在解析期暴露,浏览器专属模块(main.js/theme.js)也覆盖;
//   2. 导入检查:对可在 Node 中导入的模块执行动态 import,
//      导出名错误 / 循环导入 / 顶层副作用崩溃在加载期暴露。
//
// main.js 与 theme.js 顶层直接访问 document/window,跳过导入检查。
//
// 用法:
//   node tests/check_frontend.mjs            # 检查 assets/js 下全部模块
//   node tests/check_frontend.mjs <file...>  # 检查指定文件

import { readdirSync, readFileSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const JS_DIR = join(__dirname, '..', 'marxist-rag-ui', 'assets', 'js');

// 依赖浏览器全局的模块:顶层访问 document/window,Node 导入必炸
const BROWSER_ONLY = new Set(['main.js', 'theme.js']);

function defaultTargets() {
  return readdirSync(JS_DIR)
    .filter((f) => f.endsWith('.js'))
    .map((f) => join(JS_DIR, f))
    .sort();
}

const files = process.argv.length > 2
  ? process.argv.slice(2)
  : defaultTargets();

let failed = 0;
// SourceTextModule 需要 Node 以 --experimental-vm-modules 启动
// (本地 Node 24 / CI Node 20 均已验证)。不带 flag 时自动退化为
// 仅做导入检查,浏览器专属模块显示 SKIP。
const hasSyntaxCheck = typeof vm.SourceTextModule === 'function';
if (!hasSyntaxCheck) {
  console.log('提示: 未启用 --experimental-vm-modules,'
    + '浏览器专属模块将跳过语法检查');
}

for (const f of files) {
  const abs = resolve(f);
  const name = f.replace(/\\/g, '/').split('/').pop();
  let ok = true;
  let skipped = false;

  // 1. 语法解析(全部文件)
  if (hasSyntaxCheck) {
    try {
      const src = readFileSync(abs, 'utf-8');
      new vm.SourceTextModule(src);
    } catch (e) {
      ok = false;
      console.log('FAIL ' + f + ' [syntax]: ' + String(e.message).split('\n')[0]);
    }
  } else if (BROWSER_ONLY.has(name)) {
    skipped = true;
  }

  // 2. 导入检查(可导入模块)
  if (ok && !skipped && !BROWSER_ONLY.has(name)) {
    try {
      await import(pathToFileURL(abs).href + '?check=' + Date.now());
    } catch (e) {
      ok = false;
      console.log('FAIL ' + f + ' [import]: ' + String(e.message).split('\n')[0]);
    }
  }

  if (ok) {
    console.log((skipped ? 'SKIP ' : 'OK   ') + f);
  } else {
    failed += 1;
  }
}

console.log(failed ? `FAILED ${failed}/${files.length}` : `ALL OK (${files.length})`);
process.exit(failed ? 1 : 0);
