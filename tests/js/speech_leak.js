/* Feed a reply that carries a focus directive through the real splitter, one
   token at a time, and see what would have been spoken. */
const fs = require('fs');
const face = fs.readFileSync(require('path').join(__dirname, '..', '..', 'suni', 'web', 'face.html'), 'utf8');

function grab(name) {
  const i = face.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('not found: ' + name);
  // to the line that closes it at column 0
  const end = face.indexOf('\n}', i);
  return face.slice(i, end + 2);
}

let ttsOn = true, _ttsFirst = true;
const spoken = [];
function _enqueueSentence(s) { spoken.push(s); }
function showSub(s) { spoken.push(s); }
function _stripServeUrls(t) { return t.replace(/https?:\/\/\S*\/api\/files\/serve\S*/g, ''); }

eval(grab('_parseFocus'));
eval(grab('_emitSentences'));
eval(grab('_speakable'));

const REPLY =
  'I can see the mug on the left of the desk. ' +
  '```suni-focus {"image": 1, "box": [120, 340, 220, 180], "label": "mug"}``` ' +
  'The lighting is quite low, so the edges are soft. ' +
  'Would you like me to describe the rest of the room?';

// Stream it in awkward chunks, so the directive is split across several.
let buf = '';
for (let i = 0; i < REPLY.length; i += 7) {
  buf += REPLY.slice(i, i + 7);
  buf = _emitSentences(buf, () => {});
}
const tail = buf.trim();
if (tail.length > 1) spoken.push(tail);

const heard = spoken.join(' ');
console.log('--- what she would say ---');
console.log(heard.replace(/\s+/g, ' ').trim());
console.log('--- checks ---');
const bad = ['suni-focus', '"box"', '120', '340', '{', '}'];
let ok = true;
for (const b of bad) {
  const hit = heard.includes(b);
  if (hit) ok = false;
  console.log((hit ? 'LEAKED  ' : 'clean   ') + JSON.stringify(b));
}
// And the words either side of the directive must both survive.
for (const want of ['mug on the left', 'lighting is quite low', 'rest of the room']) {
  const hit = heard.includes(want);
  if (!hit) ok = false;
  console.log((hit ? 'kept    ' : 'LOST    ') + JSON.stringify(want));
}
// The non-streaming path (Claude Code hands the reply over whole).
const whole = _speakable(REPLY);
const wholeOk = !whole.includes('suni-focus') && whole.includes('mug on the left');
console.log((wholeOk ? 'clean   ' : 'LEAKED  ') + '"whole reply at once"');
process.exit(ok && wholeOk ? 0 : 1);
