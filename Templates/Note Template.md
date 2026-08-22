<%*
// Authored = something you wrote yourself (memo, idea, draft, original analysis).
// Captured = info collected from an external source (excerpt, copy/paste, reference material).
// Software = vendor/product tracking; keeps its own structured fields.
const types = ["Authored", "Captured", "Software"];
const choice = await tp.system.suggester(types, types, false, "Note type:");
const noteType = choice || "Authored";
const isSoftware = noteType === "Software";
const now = tp.date.now("YYYY-MM-DDTHH:mm");
-%>
---
categories:
  - "[[Creations]]"
title:
type: <% noteType %>
created: <% now %>
updated: <% now %>
classification: internal-use-only
<% isSoftware ? `vendor:\nproduct:\nversion:\nlicense-type:\ncontract-expiration:\nannual-cost:\nowner:` : `` %>
tags: []
---
<% isSoftware ? `## Overview\n\n\n## Environment\n\n\n## Integrations\n\n\n## Notes` : `## Notes` %>

