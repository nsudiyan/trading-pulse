const fs = require('node:fs');
const path = require('node:path');
const files = ['index.html', 'app.js', 'style.css', 'lib/model.js', 'lib/presentation.js', 'api/feed.js', 'api/research.js'];
for (const file of files) if (!fs.existsSync(path.join(process.cwd(), file))) throw Error(`missing ${file}`);
for (const file of ['app.js', 'lib/model.js', 'lib/presentation.js', 'api/feed.js', 'api/research.js']) new Function(fs.readFileSync(path.join(process.cwd(), file), 'utf8'));
console.log('static and serverless source check OK');
