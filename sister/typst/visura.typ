// SISTER document PDF. All content comes from data.json (built by sister/pdf_typst.py): every string is data, never
// markup, so nothing in a visura (names, addresses, "#", "$", "*") can be interpreted as Typst code.
//
// data.json = {title, subtitle, footer, meta: [[label, value]], sections: [{title, badges: [str], items: [item]}]}
// item.kind: kv | tiles | table | text | note | mutations | map

#let d = json("data.json")

#let accent = rgb("#0e7490")
#let ink = luma(25)
#let muted = luma(105)
#let rule = luma(208)
#let wash = luma(248)

#set document(title: d.title, author: "SISTER · AECS4U")
#set page(
  paper: "a4",
  flipped: true,
  margin: (x: 10mm, top: 11mm, bottom: 14mm),
  footer: context [
    #set text(6.5pt, fill: muted)
    #d.footer #h(1fr) #counter(page).display("1 / 1", both: true)
  ],
)
#set text(font: ("Inter", "Liberation Sans", "Noto Sans", "DejaVu Sans"), size: 8pt, lang: "it", fill: ink)
#set par(leading: 0.5em, spacing: 0.7em)
#set table(stroke: 0.4pt + rule, inset: (x: 3pt, y: 2.4pt))

#let caption(t) = text(6.3pt, weight: "semibold", fill: muted, tracking: 0.04em, upper(t))

#let cell(c) = if type(c) == dictionary {
  text(fill: if c.at("m", default: false) { muted } else { ink }, size: if c.at("m", default: false) { 6.8pt } else { 8pt })[#c.t]
} else { c }

#let section(s) = {
  v(5pt)
  block(sticky: true, below: 3pt, {
    text(10pt, weight: "bold", fill: accent)[#s.title]
    for b in s.badges {
      h(4pt)
      box(fill: wash, stroke: 0.4pt + rule, radius: 2pt, inset: (x: 3pt, y: 1.2pt), text(6.5pt, fill: muted)[#b])
    }
    v(-2pt)
    line(length: 100%, stroke: 0.7pt + accent)
  })
}

#let tile(t) = block(
  width: 100%, breakable: false, inset: 4.5pt, radius: 2pt, stroke: 0.5pt + rule, fill: wash,
)[
  #caption(t.label) \
  #text(
    size: if t.at("missing", default: false) { 7.2pt } else { 9pt },
    weight: if t.at("missing", default: false) { "regular" } else { "semibold" },
    fill: if t.at("missing", default: false) { muted } else { ink },
  )[#t.value]
  #if t.at("note", default: "") != "" [ \ #text(6.2pt, fill: muted)[#t.note] ]
]

#let data-table(it) = {
  if it.at("title", default: "") != "" {
    block(sticky: true, below: 2pt, text(8pt, weight: "semibold")[#it.title])
  }
  table(
    columns: it.widths.map(w => eval(w)),
    align: it.align.map(a => if a == "right" { right } else { left }) ,
    fill: (_, y) => if y == 0 { wash } else { none },
    table.header(..it.header.map(h => caption(h))),
    ..it.rows.flatten().map(cell),
  )
  v(4pt)
}

#let mutation(m) = block(
  width: 100%, breakable: false, below: 4pt, radius: 2pt, clip: true,
  stroke: 0.6pt + (if m.current { accent } else { rule }),
)[
  #block(width: 100%, fill: if m.current { accent.lighten(88%) } else { wash }, inset: (x: 5pt, y: 3pt))[
    #if m.current [
      #box(fill: accent, radius: 2pt, inset: (x: 3pt, y: 1.4pt), text(6.3pt, fill: white, weight: "bold")[ATTUALE])
      #h(2pt)
    ]
    #text(weight: "semibold")[#("#" + m.index)] #h(3pt) #m.act
    #h(1fr) #text(fill: muted, style: "italic")[#m.date]
  ]
  #pad(x: 5pt, y: 3pt, table(
    columns: (2.4fr, 1.5fr, 1.6fr, 0.6fr),
    stroke: none,
    inset: (x: 2pt, y: 2pt),
    ..m.owners.map(o => (
      [#text(weight: "semibold")[#o.name] #if o.sub != "" [ \ #text(6.5pt, fill: muted)[#o.sub] ] #if o.cf != "" [ \ #text(6.5pt, font: "DejaVu Sans Mono")[#o.cf] ]],
      [#box(fill: luma(228), radius: 2pt, inset: (x: 3pt, y: 1.2pt), text(7pt)[#o.right])],
      text(7pt, fill: muted)[#o.period],
      align(right, box(fill: accent.lighten(80%), radius: 2pt, inset: (x: 3pt, y: 1.2pt), text(7pt, weight: "semibold")[#o.quota])),
    )).flatten(),
  ))
]

#let parcel-map(it) = {
  grid(
    columns: (1fr,) * calc.min(it.panels.len(), 3),
    gutter: 6pt,
    ..it.panels.map(p => block(breakable: false, width: 100%, stroke: 0.5pt + rule, radius: 2pt, inset: 4pt)[
      #text(7.5pt, weight: "semibold")[#p.title] \
      #box(width: 100%, height: p.h * 1mm, fill: luma(252), {
        for poly in p.polygons {
          place(top + left, polygon(
            fill: if poly.selected { accent.lighten(55%) } else { luma(225) },
            stroke: 0.5pt + (if poly.selected { accent } else { muted }),
            ..poly.points.map(pt => (pt.at(0) * 1mm, pt.at(1) * 1mm)),
          ))
        }
        for poly in p.polygons {
          place(top + left, dx: poly.lx * 1mm, dy: poly.ly * 1mm, text(5.5pt, fill: ink, weight: "semibold")[#poly.label])
        }
        for dot in p.dots {
          place(top + left, dx: dot.x * 1mm - 1.5pt, dy: dot.y * 1mm - 1.5pt, circle(radius: 1.5pt, fill: accent))
          place(top + left, dx: dot.x * 1mm + 3pt, dy: dot.y * 1mm - 3pt, text(5.5pt)[#dot.label])
        }
      })
      #text(6pt, fill: muted)[#p.note]
    ]),
  )
  v(4pt)
}

// ── header ───────────────────────────────────────────────────────────────────────────────────────────────
#block(width: 100%, below: 6pt)[
  #text(16pt, weight: "bold")[#d.title] \
  #text(9pt, fill: muted)[#d.subtitle]
  #h(1fr)
]
#grid(
  columns: (auto,) * d.meta.len(),
  column-gutter: 14pt,
  ..d.meta.map(m => [#caption(m.at(0)) \ #text(8.5pt)[#m.at(1)]]),
)

// ── body ─────────────────────────────────────────────────────────────────────────────────────────────────
#for s in d.sections {
  section(s)
  for it in s.items {
    if it.kind == "kv" {
      grid(
        columns: (1fr,) * calc.min(it.rows.len(), it.at("columns", default: 4)),
        gutter: 6pt,
        ..it.rows.map(r => block(breakable: false)[#caption(r.at(0)) \ #text(8.5pt, weight: "semibold")[#r.at(1)] #if r.at(2, default: "") != "" [ \ #text(6.8pt, fill: muted)[#r.at(2)] ]]),
      )
      v(4pt)
    } else if it.kind == "tiles" {
      grid(columns: (1fr,) * it.columns, gutter: 4pt, ..it.tiles.map(tile))
      v(4pt)
    } else if it.kind == "table" {
      data-table(it)
    } else if it.kind == "mutations" {
      block(sticky: true, below: 2pt, text(8pt, weight: "semibold")[#it.title])
      for m in it.items { mutation(m) }
    } else if it.kind == "map" {
      parcel-map(it)
    } else if it.kind == "note" {
      text(6.8pt, fill: muted)[#it.text]
      parbreak()
    } else if it.kind == "text" {
      text(8pt)[#it.text]
      parbreak()
    }
  }
}
