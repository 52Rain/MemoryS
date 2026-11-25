import assert from 'node:assert/strict';
import { renderMarkdown } from '../desktop-app/src/markdown.js';

for (const info of ['c++', 'python title=x', 'ts:app.ts']) {
  const html = renderMarkdown(`\`\`\`${info}\n<script>\n\`\`\`\n\nAfter`);
  assert.equal(html, '<pre><code>&lt;script&gt;</code></pre>\n<p>After</p>');
}

for (const source of [
  '\`\`\`unterminated title=x\n<script>',
  'intro\n- line\u2028break\ntext',
  'intro\n## line\u2028break\ntext',
  ('> <script>\n\n\`\`\`c++\n<script>\n\`\`\`\n').repeat(1000),
]) {
  const html = renderMarkdown(source);
  assert.ok(html.length);
  assert.ok(!html.includes('<script>'));
}
console.log('Passed 3 fenced-code headers and 4 parser progress / escaping cases.');
