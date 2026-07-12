/**
 * generate_resume_docx.js -- Phase 5: produce a clean, ATS-parseable resume .docx
 *
 * Called by resume_tailor.py:
 *   node generate_resume_docx.js <tailored_content.json> <output_path.docx>
 *
 * ATS-parseable design decisions (from skill.md + ATS research):
 *   - Single column layout (multi-column breaks most ATS parsers)
 *   - No tables for layout (only for genuine tabular content)
 *   - No text boxes, no headers/footers for key content
 *   - Standard section headings (EXPERIENCE, EDUCATION, SKILLS, PROJECTS)
 *   - Bullet points via docx-js numbering config (not unicode bullets)
 *   - US Letter page size, 1-inch margins
 *   - Arial 11pt body, 12pt name, consistent sizing
 */

const {
  Document, Packer, Paragraph, TextRun, AlignmentType,
  LevelFormat, HeadingLevel, BorderStyle, WidthType, UnderlineType,
  ExternalHyperlink
} = require('docx');
const fs = require('fs');

const args = process.argv.slice(2);
if (args.length < 2) {
  console.error('Usage: node generate_resume_docx.js <content.json> <output.docx>');
  process.exit(1);
}

const content = JSON.parse(fs.readFileSync(args[0], 'utf8'));
const outputPath = args[1];

const BLUE = "1F4E79";
const DARK = "1A1A1A";
const GRAY = "555555";
const LINE_COLOR = "2E75B6";

// ---- helpers ---------------------------------------------------------------

function sectionHeading(text) {
  return new Paragraph({
    children: [new TextRun({ text: text.toUpperCase(), bold: true, size: 22, color: BLUE, font: "Arial" })],
    spacing: { before: 200, after: 80 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: LINE_COLOR, space: 1 } },
  });
}

function bulletPara(text) {
  return new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    children: [new TextRun({ text, size: 20, font: "Arial", color: DARK })],
    spacing: { before: 40, after: 40 },
  });
}

function spacer(before = 80) {
  return new Paragraph({ children: [new TextRun("")], spacing: { before, after: 0 } });
}

// ---- sections ---------------------------------------------------------------

function buildHeader(candidate) {
  const paras = [];

  // Name
  paras.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: candidate.name, bold: true, size: 32, font: "Arial", color: DARK })],
    spacing: { before: 0, after: 80 },
  }));

  // Contact line
  const contacts = [];
  if (candidate.phone) contacts.push(candidate.phone);
  if (candidate.email) contacts.push(candidate.email);

  const contactChildren = [];
  contacts.forEach((c, i) => {
    contactChildren.push(new TextRun({ text: c, size: 18, font: "Arial", color: GRAY }));
    if (i < contacts.length - 1)
      contactChildren.push(new TextRun({ text: "  |  ", size: 18, font: "Arial", color: GRAY }));
  });
  paras.push(new Paragraph({ alignment: AlignmentType.CENTER, children: contactChildren, spacing: { after: 60 } }));

  // Links line
  const linkChildren = [];
  if (candidate.linkedin) {
    linkChildren.push(new ExternalHyperlink({
      link: candidate.linkedin,
      children: [new TextRun({ text: "LinkedIn", size: 18, font: "Arial", color: "0563C1", underline: { type: UnderlineType.SINGLE } })],
    }));
    if (candidate.github)
      linkChildren.push(new TextRun({ text: "  |  ", size: 18, font: "Arial", color: GRAY }));
  }
  if (candidate.github) {
    linkChildren.push(new ExternalHyperlink({
      link: candidate.github,
      children: [new TextRun({ text: "GitHub", size: 18, font: "Arial", color: "0563C1", underline: { type: UnderlineType.SINGLE } })],
    }));
  }
  if (linkChildren.length > 0)
    paras.push(new Paragraph({ alignment: AlignmentType.CENTER, children: linkChildren, spacing: { after: 60 } }));

  return paras;
}

function buildEducation(education) {
  const paras = [sectionHeading("Education")];
  education.forEach(ed => {
    paras.push(new Paragraph({
      children: [
        new TextRun({ text: ed.college || "", bold: true, size: 22, font: "Arial", color: DARK }),
        new TextRun({ text: `  |  ${ed.years || ""}`, size: 20, font: "Arial", color: GRAY }),
      ],
      spacing: { before: 100, after: 40 },
    }));
    paras.push(new Paragraph({
      children: [
        new TextRun({ text: ed.degree || "", size: 20, font: "Arial", color: DARK }),
        ed.cgpa ? new TextRun({ text: `  |  CGPA: ${ed.cgpa}`, size: 20, font: "Arial", color: GRAY }) : new TextRun(""),
      ],
      spacing: { after: 60 },
    }));
  });
  return paras;
}

function buildSkills(skills) {
  const paras = [sectionHeading("Technical Skills")];
  // Display skills as a comma-separated line (ATS-safe, no table)
  paras.push(new Paragraph({
    children: [new TextRun({ text: skills.join("  •  "), size: 20, font: "Arial", color: DARK })],
    spacing: { before: 80, after: 60 },
  }));
  return paras;
}

function buildProjects(projects) {
  const paras = [sectionHeading("Projects")];
  projects.forEach(proj => {
    // Project title + duration on same line
    paras.push(new Paragraph({
      children: [
        new TextRun({ text: proj.title || "", bold: true, size: 22, font: "Arial", color: DARK }),
        proj.duration ? new TextRun({ text: `  |  ${proj.duration}`, size: 20, font: "Arial", color: GRAY }) : new TextRun(""),
      ],
      spacing: { before: 120, after: 50 },
    }));

    const bullets = proj.bullets || [];
    bullets.forEach(b => {
      if (b && b.trim()) paras.push(bulletPara(b.trim().replace(/^[\u2022•\-]\s*/, "")));
    });
  });
  return paras;
}

// ---- document assembly -----------------------------------------------------

function buildDocument(content) {
  const children = [
    ...buildHeader(content.candidate),
    spacer(60),
    ...buildEducation(content.education || []),
    spacer(60),
    ...buildSkills(content.skills || []),
    spacer(60),
    ...buildProjects(content.projects || []),
  ];

  return new Document({
    numbering: {
      config: [{
        reference: "bullets",
        levels: [{
          level: 0,
          format: LevelFormat.BULLET,
          text: "\u2022",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 360, hanging: 220 } } },
        }],
      }],
    },
    styles: {
      default: { document: { run: { font: "Arial", size: 20, color: DARK } } },
    },
    sections: [{
      properties: {
        page: {
          size: { width: 12240, height: 15840 },
          margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 },
        },
      },
      children,
    }],
  });
}

// ---- main ------------------------------------------------------------------

const doc = buildDocument(content);
Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync(outputPath, buffer);
  console.log(`Written: ${outputPath}`);
}).catch(err => {
  console.error('Error generating docx:', err);
  process.exit(1);
});