// Copy static native SVGs without importing or executing frontend source code.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const frontend = path.resolve(process.argv[2] || '');
if (!process.argv[2]) throw new Error('Usage: node export_action_icons.cjs <frontend-repository>');
const parser = require(path.join(frontend, 'node_modules/@babel/parser'));
const sourceDir = path.join(frontend, 'src/core/common/ActionDesign/Element');
const output = path.resolve(__dirname, '../assets/action-icons');
fs.mkdirSync(output, { recursive: true });
const manifest = { source: 'ActionDesign element.image (static SVG)', icons: {}, skipped: [] };
const escape = value => String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
function literal(node) {
  if (['StringLiteral', 'NumericLiteral', 'BooleanLiteral'].includes(node?.type)) return node.value;
  throw new Error('Nonliteral SVG attribute');
}
function attrName(name) {
  if (name === 'className') return 'class';
  if (['viewBox', 'preserveAspectRatio'].includes(name)) return name;
  if (name === 'xmlnsXlink') return 'xmlns:xlink';
  if (name === 'xlinkHref') return 'xlink:href';
  return name.replace(/[A-Z]/g, char => '-' + char.toLowerCase());
}
function svg(node) {
  if (node.type === 'JSXText') return escape(node.value.trim());
  if (node.type === 'JSXExpressionContainer') return escape(literal(node.expression));
  if (node.type !== 'JSXElement' || node.openingElement.name.type !== 'JSXIdentifier') throw new Error('Nonstatic SVG');
  const tag = node.openingElement.name.name;
  if (!['svg', 'path', 'g', 'circle', 'ellipse', 'rect', 'polygon', 'polyline', 'line', 'defs', 'clipPath', 'linearGradient', 'radialGradient', 'stop', 'use', 'title'].includes(tag)) throw new Error('Unsupported SVG tag: ' + tag);
  const attrs = node.openingElement.attributes.map(attr => {
    if (attr.type !== 'JSXAttribute' || attr.name.type !== 'JSXIdentifier') throw new Error('Dynamic SVG attributes');
    const name = attrName(attr.name.name);
    if (/^on/i.test(name)) throw new Error('SVG handler');
    let value;
    if (attr.value?.type === 'JSXExpressionContainer') {
      if (name === 'style' && attr.value.expression.type === 'ObjectExpression') {
        value = attr.value.expression.properties.map(p => attrName(p.key.name || literal(p.key)) + ':' + literal(p.value)).join(';');
      } else value = literal(attr.value.expression);
    } else value = literal(attr.value);
    if ((name === 'href' || name === 'xlink:href') && !String(value).startsWith('#')) throw new Error('External SVG reference');
    return `${name}="${escape(value)}"`;
  });
  if (tag === 'svg') {
    if (!attrs.some(a => a.startsWith('xmlns='))) attrs.push('xmlns="http://www.w3.org/2000/svg"');
    if (!attrs.some(a => a.startsWith('fill='))) attrs.push('fill="#666666"');
    attrs.push('color="#666666"');
  }
  return `<${tag} ${attrs.join(' ')}>${node.children.map(svg).join('')}</${tag}>`;
}
function visit(node, file) {
  if (!node || typeof node !== 'object') return;
  if (node.type === 'ObjectExpression') {
    const props = node.properties.filter(p => p.type === 'ObjectProperty');
    const key = props.find(p => (p.key.name || p.key.value) === 'key')?.value;
    const image = props.find(p => (p.key.name || p.key.value) === 'image')?.value;
    if (key?.type === 'StringLiteral' && image) {
      try {
        if (!/^[A-Za-z0-9_]+$/.test(key.value)) throw new Error('Invalid element key');
        let xml;
        if (image.type === 'StringLiteral' && image.value.startsWith('data:image/svg+xml;base64,')) {
          const decoded = Buffer.from(image.value.split(',')[1], 'base64').toString('utf8');
          xml = decoded.match(/<svg\b[\s\S]*<\/svg>/)?.[0];
          if (!xml || /<(script|foreignObject)\b|\bon\w+\s*=|<!ENTITY/i.test(xml)) throw new Error('Unsupported SVG data URI');
        } else xml = svg(image);
        fs.writeFileSync(path.join(output, key.value + '.svg'), xml + '\n');
        manifest.icons[key.value] = { source: path.relative(frontend, file).replace(/\\/g, '/'),
          sha256: crypto.createHash('sha256').update(xml + '\n').digest('hex') };
      } catch (error) { manifest.skipped.push({ key: key.value, reason: error.message }); }
    }
  }
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach(item => visit(item, file));
    else if (value && typeof value === 'object') visit(value, file);
  }
}
function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(file);
    else if (/\.tsx?$/.test(entry.name)) {
      try { visit(parser.parse(fs.readFileSync(file, 'utf8'), { sourceType: 'module', plugins: ['typescript', 'jsx'] }), file); }
      catch (error) { manifest.skipped.push({ file: path.relative(sourceDir, file), reason: error.message }); }
    }
  }
}
walk(sourceDir);
fs.writeFileSync(path.join(output, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(JSON.stringify({ copied: Object.keys(manifest.icons).length, skipped: manifest.skipped }));
