/**
 * generate_cover_letter_docx.js -- produce a clean cover letter .docx
 *
 * Called by cover_letter.py:
 *   node generate_cover_letter_docx.js <content.json> <output_path.docx>
 *
 * Same design language as generate_resume_docx.js: single column,
 * US Letter, 1-inch margins, Arial.
 */

const { Document, Packer, Paragraph, TextRun, AlignmentType } = require('docx');
const fs = require('fs');

const args = process.argv.slice(2);
if (args.length < 2) {
  console.error('Usage: node generate_cover_letter_docx.js <content.json> <output.docx>');
  process.exit(1);
}

const content = JSON.parse(fs.readFileSync(args[0], 'utf8'));
const outputPath = args[1];

const DARK = "1A1A1A";
const GRAY = "555555";

function bodyPara(text, opts = {}) {
  return new Paragraph({
    children: [new TextRun({ text, size: 22, font: "Arial", color: DARK, ...opts })],
    spacing: { before: 0, after: 200 },
  });
}

const c = content.candidate || {};
const children = [];

// Header: name + contact
children.push(new Paragraph({
  children: [new TextRun({ text: c.name || "", bold: true, size: 28, font: "Arial", color: DARK })],
  spacing: { before: 0, after: 40 },
}));
const contact = [c.email, c.phone, c.linkedin].filter(Boolean).join("  |  ");
children.push(new Paragraph({
  children: [new TextRun({ text: contact, size: 18, font: "Arial", color: GRAY })],
  spacing: { before: 0, after: 300 },
}));

// Date
children.push(bodyPara(new Date().toLocaleDateString('en-US', {
  year: 'numeric', month: 'long', day: 'numeric'
})));

// Subject line
children.push(new Paragraph({
  children: [new TextRun({
    text: `Re: ${content.target_role || ""} — ${content.target_company || ""}`,
    bold: true, size: 22, font: "Arial", color: DARK
  })],
  spacing: { before: 0, after: 300 },
}));

// Salutation
children.push(bodyPara("Dear Hiring Team,"));

// Body paragraphs
(content.paragraphs || []).forEach(p => children.push(bodyPara(p)));

// Sign-off
children.push(bodyPara("Sincerely,"));
children.push(bodyPara(c.name || ""));

const doc = new Document({
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },           // US Letter (twips)
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      },
    },
    children,
  }],
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync(outputPath, buffer);
  console.log(`Cover letter written to ${outputPath}`);
}).catch(err => {
  console.error('Failed to generate docx:', err);
  process.exit(1);
});
