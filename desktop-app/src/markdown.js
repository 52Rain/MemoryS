/** Safe zero-dependency Markdown renderer for MemoryS desktop cards and proposals. */

function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function safeUrl(raw) {
  const trimmed = String(raw || '').trim();
  if (/^https?:\/\/[^\s<>"']+$/i.test(trimmed)) {
    return trimmed;
  }
  return '';
}

function renderInline(raw) {
  const tokens = [];
  const stash = (html) => {
    const key = `\u0000T${tokens.length}\u0000`;
    tokens.push(html);
    return key;
  };

  let text = String(raw ?? '');

  // 1. Protect inline code spans first
  text = text.replace(/`([^`\n]+)`/g, (_, code) =>
    stash(`<code>${escapeHtml(code)}</code>`)
  );

  // 2. Protect markdown links [label](https://...)
  text = text.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, href) => {
    const cleanHref = safeUrl(href);
    if (!cleanHref) return escapeHtml(label);
    return stash(
      `<a href="${escapeHtml(cleanHref)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`
    );
  });

  // 3. Protect bare URLs
  text = text.replace(/(^|[\s(>（【：:])((?:https?:\/\/)[^\s<>"')）\]】、，。；;！!？?]+)/g, (_, lead, href) => {
    const cleanHref = safeUrl(href);
    if (!cleanHref) return `${escapeHtml(lead)}${escapeHtml(href)}`;
    return (
      escapeHtml(lead) +
      stash(
        `<a href="${escapeHtml(cleanHref)}" target="_blank" rel="noopener noreferrer">${escapeHtml(cleanHref)}</a>`
      )
    );
  });

  // 4. Escape remaining text
  text = escapeHtml(text);

  // 5. Bold and italic
  text = text.replace(/\*\*([^\n*](?:[^\n]*?[^\n*])?)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/(^|[^*])\*([^\s*\n](?:[^\n]*?[^\s*\n])?)\*(?!\*)/g, '$1<em>$2</em>');

  // 6. Restore stashed inline HTML tokens
  text = text.replace(/\u0000T(\d+)\u0000/g, (_, idx) => tokens[Number(idx)] ?? '');
  return text;
}

function parseListBlock(lines, startIdx) {
  const items = [];
  const rootStack = [];
  let i = startIdx;

  const listMatch = (line) => {
    const m = line.match(/^([ \t]*)(?:([-*+])|(\d+[.)]))\s+(.*)$/);
    if (!m) return null;
    const indent = m[1].replace(/\t/g, '  ').length;
    const ordered = Boolean(m[3]);
    return { indent, ordered, content: m[4] };
  };

  const renderTree = (nodes) => {
    if (!nodes.length) return '';
    const tag = nodes[0].ordered ? 'ol' : 'ul';
    const body = nodes
      .map((node) => {
        const sub = node.children.length ? renderTree(node.children) : '';
        const textHtml = node.lines.map((l) => renderInline(l)).join('<br />');
        return `<li>${textHtml}${sub}</li>`;
      })
      .join('');
    return `<${tag}>${body}</${tag}>`;
  };

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // Look ahead: continue list only if next non-empty line is also a list item of the same top-level ordered type
      let look = i + 1;
      while (look < lines.length && !lines[look].trim()) look += 1;
      if (look < lines.length) {
        const nextMatch = listMatch(lines[look]);
        if (nextMatch && (!items.length || nextMatch.indent > items[0].indent || nextMatch.ordered === items[0].ordered)) {
          i = look;
          continue;
        }
      }
      break;
    }

    const matched = listMatch(line);
    if (!matched) {
      // Continuation line for previous item if indented
      if (items.length && /^[ \t]{2,}\S/.test(line) && !/^#{1,6}\s/.test(line.trim())) {
        rootStack[rootStack.length - 1].node.lines.push(line.trim());
        i += 1;
        continue;
      }
      break;
    }

    if (items.length && matched.indent <= items[0].indent && matched.ordered !== items[0].ordered) {
      break;
    }

    const node = {
      indent: matched.indent,
      ordered: matched.ordered,
      lines: [matched.content],
      children: [],
    };

    while (rootStack.length && matched.indent < rootStack[rootStack.length - 1].indent) {
      rootStack.pop();
    }

    if (!rootStack.length) {
      items.push(node);
      rootStack.push({ indent: matched.indent, node });
    } else {
      const top = rootStack[rootStack.length - 1];
      if (matched.indent > top.indent) {
        top.node.children.push(node);
        rootStack.push({ indent: matched.indent, node });
      } else {
        // Same indent level as top -> attach to parent of top
        rootStack.pop();
        if (rootStack.length) {
          rootStack[rootStack.length - 1].node.children.push(node);
        } else {
          items.push(node);
        }
        rootStack.push({ indent: matched.indent, node });
      }
    }
    i += 1;
  }

  return { html: renderTree(items), nextIdx: i };
}

export function renderMarkdown(source) {
  const raw = String(source ?? '').replace(/\r\n?/g, '\n').trim();
  if (!raw) return '';

  const lines = raw.split('\n');
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    // Fenced code block
    const fence = trimmed.match(/^```([\w-]*)\s*$/);
    if (fence) {
      const codeLines = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      out.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
      continue;
    }

    // Horizontal rule
    if (/^(?:---+|___+|\*\*\*+)$/.test(trimmed)) {
      out.push('<hr />');
      i += 1;
      continue;
    }

    // Heading (# .. #### -> h3/h4)
    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length <= 2 ? 'h3' : 'h4';
      out.push(`<${level}>${renderInline(heading[2].trim())}</${level}>`);
      i += 1;
      continue;
    }

    // Blockquote
    if (trimmed.startsWith('>')) {
      const quoteLines = [];
      while (i < lines.length && lines[i].trim().startsWith('>')) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, ''));
        i += 1;
      }
      out.push(`<blockquote>${quoteLines.map((l) => renderInline(l)).join('<br />')}</blockquote>`);
      continue;
    }

    // List
    if (/^[ \t]*(?:[-*+]|\d+[.)])\s+/.test(line)) {
      const parsed = parseListBlock(lines, i);
      out.push(parsed.html);
      i = parsed.nextIdx;
      continue;
    }

    // Paragraph (stop before special blocks)
    const paraLines = [];
    while (i < lines.length) {
      const cur = lines[i];
      const curTrim = cur.trim();
      if (
        !curTrim ||
        curTrim.startsWith('```') ||
        curTrim.startsWith('>') ||
        /^(?:---+|___+|\*\*\*+)$/.test(curTrim) ||
        /^#{1,6}\s+/.test(curTrim) ||
        /^[ \t]*(?:[-*+]|\d+[.)])\s+/.test(cur)
      ) {
        break;
      }
      paraLines.push(curTrim);
      i += 1;
    }
    out.push(`<p>${paraLines.map((l) => renderInline(l)).join('<br />')}</p>`);
  }

  return out.join('\n');
}
