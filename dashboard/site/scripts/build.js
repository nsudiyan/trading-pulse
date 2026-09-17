const fs = require('node:fs');
const path = require('node:path');
const out = path.join(process.cwd(), 'dist');
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });
for (const file of ['index.html', 'app.js', 'style.css', 'feed.json', 'manifest.webmanifest', 'sw.js', 'icon-180.png', 'icon-192.png', 'icon-512.png']) fs.copyFileSync(path.join(process.cwd(), file), path.join(out, file));
console.log(`built ${out}`);
