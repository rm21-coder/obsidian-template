#!/usr/bin/env node
/*
 * meeting_handoff_transform.js — reference PRODUCER transform for the meeting
 * pre-population pipeline.
 *
 * This is the deterministic step that turns raw Microsoft Graph calendar +
 * directory data into the schema-version-1 handoff JSON that the consumer
 * (Templates/Scripts/meeting_prepopulate.py) reads. Reproduce its output
 * byte-for-byte and the consumer + checksum flow "just work".
 *
 * It is a REFERENCE implementation. The surrounding job (gather calendar,
 * batch the directory reads, deliver into OneDrive) is described in
 * docs/Meeting-Pre-Population-Producer.md and can be built with Microsoft
 * Cowork/Copilot, Power Automate, a Graph script, or any agent — this file is
 * only the transform.
 *
 * Runtime: Node.js. Inputs (in ./working/):
 *   working/calendar.json         — Graph calendarView events for the day
 *   working/batch_responses.json  — Graph $batch directory profile responses
 * Outputs:
 *   working/payload.json          — the handoff (pretty-printed)
 *   working/payload.sha256        — SHA-256 of the exact payload bytes
 *
 * Configure for your tenant with env vars (see the entry point at the bottom):
 *   PREPOP_USER_NAME, PREPOP_USER_EMAIL, PREPOP_TENANT, PREPOP_TIMEZONE,
 *   PREPOP_TENANT_DOMAINS (comma-separated), PREPOP_RUN_DATE (YYYY-MM-DD).
 */
const fs = require('fs');
const crypto = require('crypto');

// ===== buildProfiles: Graph $batch responses[] -> profiles map (keyed by lc email) =====
function buildProfiles(responses) {
  function lc(s){ return (s||"").toString().trim().toLowerCase(); }
  const profiles = {};
  for (const r of (responses || [])) {
    const id = r.id || "";
    const isMgr = id.indexOf("::mgr") > -1;
    const email = lc(isMgr ? id.replace("::mgr", "") : id);
    if (!email) continue;
    if (r.status === 200 && r.body) {
      if (isMgr) {
        if (!profiles[email]) profiles[email] = {};
        profiles[email].manager = { displayName: r.body.displayName || null, mail: r.body.mail || null };
      } else {
        const prev = profiles[email] || {};
        r.body.manager = prev.manager || null;
        profiles[email] = r.body;
      }
    }
  }
  return profiles;
}

// ===== main transform: raw Graph -> contract JSON =====
function buildHandoff(calendarView, profiles, user, week, tenantDomains) {
  function lc(s){ return (s||"").toString().trim().toLowerCase(); }
  function isGroupMailbox(name, email){
    const n = (name||"").trim(); const e = lc(email);
    const domain = e.split("@")[1] || ""; const local = e.split("@")[0] || "";
    if (domain.indexOf("exchange.") === 0 || domain.indexOf(".exchange.") > -1) return true;
    if (/^(grp-|dg-|dl-|org-)/i.test(n)) return true;
    if (/^office of /i.test(n)) return true;
    if (/^[A-Z]{2,6}-/.test(n) && local.indexOf(".") === -1) return true;
    return false;
  }
  function isExternal(email){ const d = lc(email).split("@")[1] || ""; return tenantDomains.indexOf(d) === -1; }
  function tzOffsetMinutes(dateUtc, tz){
    const dtf = new Intl.DateTimeFormat('en-US',{timeZone:tz,hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const p = dtf.formatToParts(dateUtc).reduce(function(a,x){a[x.type]=x.value;return a;},{});
    if(p.hour==='24') p.hour='00';
    const asUTC = Date.UTC(+p.year,+p.month-1,+p.day,+p.hour,+p.minute,+p.second);
    return (asUTC - dateUtc.getTime())/60000;
  }
  function toUtcIso(g){ if(!g) return null; let s = g.replace(" ","T"); if(s.length>=19) s = s.substring(0,19); const prov = new Date(s+"Z"); if(isNaN(prov.getTime())) return s+"Z"; const off = tzOffsetMinutes(prov, user.timezone || "America/New_York"); const utcMs = prov.getTime() - off*60000; return new Date(utcMs).toISOString().substring(0,19)+"Z"; }
  function durationMinutes(a,b){ if(!a||!b) return null; const x=new Date(a).getTime(), y=new Date(b).getTime(); if(isNaN(x)||isNaN(y)) return null; return Math.round((y-x)/60000); }
  function splitName(dn){ dn=(dn||"").trim(); if(!dn) return {given_name:null,surname:null}; if(dn.indexOf(",")>-1) return {given_name:null,surname:null}; const p=dn.split(/\s+/); if(p.length<2) return {given_name:dn,surname:null}; return {given_name:p[0],surname:p[p.length-1]}; }
  function mapRole(t){ if(t==="resource") return "resource"; if(t==="optional") return "optional"; return "required"; }
  const userEmail = lc(user.email);
  const seen = {}; const meetings = []; const notes = []; const excludedMailboxes = {};
  for (const ev of (calendarView || [])) {
    const rawAtt = ev.attendees || []; const attendees = [];
    let nReqHuman=0, nOptional=0, nDeclined=0, nResources=0, nExternal=0;
    for (const a of rawAtt) {
      const email = lc(a.emailAddress && a.emailAddress.address);
      const name = (a.emailAddress && a.emailAddress.name) || "";
      if (!email) continue;
      const role = mapRole(a.type);
      const resp = (a.status && a.status.response) || "none";
      if (isGroupMailbox(name, email)) { excludedMailboxes[name||email] = true; continue; }
      const ext = isExternal(email), optional = role==="optional", resource = role==="resource", declined = resp==="declined";
      if (resource) nResources++;
      if (optional) nOptional++;
      if (declined) nDeclined++;
      if (ext) nExternal++;
      if (!resource && !declined && !optional && email !== userEmail) nReqHuman++;
      attendees.push({ display_name:name||null, email:email, response_status:resp, role:role, is_resource:resource, is_external:ext, is_optional:optional });
      if (!resource && !seen[email]) {
        const p = (profiles && profiles[email]) || {};
        const fb = splitName(p.displayName || name);
        seen[email] = {
          email: email,
          display_name: p.displayName || name || null,
          given_name: p.givenName || fb.given_name,
          surname: p.surname || fb.surname,
          title: p.jobTitle || null,
          department: p.department || null,
          office: p.officeLocation || null,
          phone: (p.businessPhones && p.businessPhones[0]) || null,
          manager: (p.manager && (p.manager.displayName || p.manager.mail))
            ? { display_name: p.manager.displayName || null, email: lc(p.manager.mail) || null } : null,
          company: p.companyName || null,
          is_external: ext,
          directory_source: (profiles && profiles[email]) ? "tenant-gal" : "invite-fallback",
          directory_object_id: p.id || null
        };
      }
    }
    const org = ev.organizer && ev.organizer.emailAddress ? ev.organizer.emailAddress : {};
    const orgEmail = lc(org.address), orgName = org.name || ""; let organizer;
    if (isGroupMailbox(orgName, orgEmail)) {
      const humans = attendees.filter(function(x){ return !x.is_resource && x.response_status!=="declined" && x.email!==userEmail; });
      if (humans.length === 1) organizer = { display_name:humans[0].display_name, email:humans[0].email, is_me:humans[0].email===userEmail, is_group_mailbox:false };
      else organizer = { display_name:orgName||null, email:orgEmail||null, is_me:false, is_group_mailbox:true };
    } else {
      organizer = { display_name:orgName||null, email:orgEmail||null, is_me:orgEmail===userEmail, is_group_mailbox:false };
    }
    const startIso = toUtcIso(ev.start && ev.start.dateTime);
    const endIso = toUtcIso(ev.end && ev.end.dateTime);
    const sensitivity = ev.sensitivity || "normal";
    let bodyPreview = ev.bodyPreview || "";
    if (sensitivity === "private" || sensitivity === "confidential") bodyPreview = "[redacted: sensitivity=" + sensitivity + "]";
    let cls;
    if (nReqHuman === 0) cls = "solo";
    else if (nReqHuman === 1) cls = "individual";
    else if (nReqHuman >= 12) cls = "broadcast";
    else cls = "group";
    const myResp = (ev.responseStatus && ev.responseStatus.response) || "none";
    const isRecurring = ev.type === "occurrence" || ev.type === "exception" || !!ev.seriesMasterId;
    meetings.push({
      uid: ev.id || null,
      series_uid: ev.seriesMasterId || null,
      is_recurring_instance: isRecurring,
      instance_index: null, recurrence_human: null, rrule_raw: null,
      subject: ev.subject || "", subject_sensitivity: "normal",
      start: startIso, end: endIso, duration_minutes: durationMinutes(startIso, endIso), is_all_day: !!ev.isAllDay,
      organizer: organizer, my_response_status: myResp, attendees: attendees,
      attendee_counts: { required_non_declined_non_resource:nReqHuman, optional:nOptional, declined:nDeclined, resources:nResources, external:nExternal },
      location: { display:(ev.location&&ev.location.displayName)||null, is_teams_meeting:!!ev.isOnlineMeeting, teams_join_url:(ev.onlineMeeting&&ev.onlineMeeting.joinUrl)||null },
      body_preview: bodyPreview, body_format: "text",
      categories: ev.categories || [], sensitivity: sensitivity, is_cancelled: !!ev.isCancelled, is_private_appointment: sensitivity==="private",
      created_at: ev.createdDateTime || null, last_modified_at: ev.lastModifiedDateTime || null,
      producer_classification_hint: { class: cls, rationale: nReqHuman + " required non-resource human attendees; recurring=" + isRecurring }
    });
  }
  const contacts = Object.keys(seen).map(function(k){ return seen[k]; });
  const excludedList = Object.keys(excludedMailboxes);
  if (excludedList.length > 0) notes.push({ level:"info", text:"Excluded "+excludedList.length+" group/office mailboxes from attendees and contacts: "+excludedList.join(", ")+"." });
  return {
    schema_version: 1, source: "microsoft-cowork", source_version: "0.7.0", generated_at: new Date().toISOString(),
    user: { display_name:user.display_name, email:user.email, tenant:user.tenant, timezone:user.timezone },
    week: { start:week.start, end:week.end },
    meetings: meetings, contacts: contacts, notes: notes
  };
}

// ===== entry point =====
// Tenant/user values are read from the environment with generic placeholders as
// defaults. Set these for your tenant (or edit them inline).
const calendarView = JSON.parse(fs.readFileSync('working/calendar.json','utf8'));
const batchResponses = JSON.parse(fs.readFileSync('working/batch_responses.json','utf8'));
const user = {
  display_name: process.env.PREPOP_USER_NAME  || "Your Name",
  email:        process.env.PREPOP_USER_EMAIL || "you@example.edu",
  tenant:       process.env.PREPOP_TENANT     || "example.edu",
  timezone:     process.env.PREPOP_TIMEZONE   || "America/New_York"
};
const runDate = process.env.PREPOP_RUN_DATE || new Date().toISOString().substring(0,10);
const week = { start: runDate, end: runDate };
const tenantDomains = (process.env.PREPOP_TENANT_DOMAINS || "example.edu,corp.example.edu")
  .split(",").map(function(s){ return s.trim().toLowerCase(); }).filter(Boolean);
const profiles = buildProfiles(batchResponses);
const result = buildHandoff(calendarView, profiles, user, week, tenantDomains);
const payloadString = JSON.stringify(result, null, 2);
const summary = { meetingCount: result.meetings.length, contactCount: result.contacts.length };
fs.writeFileSync('working/payload.json', payloadString);
const sha = crypto.createHash('sha256').update(payloadString).digest('hex');
fs.writeFileSync('working/payload.sha256', sha);
// counts only — keep meeting/attendee content out of stdout
const gal = result.contacts.filter(c=>c.directory_source==='tenant-gal').length;
const fb = result.contacts.filter(c=>c.directory_source==='invite-fallback').length;
const withMgr = result.contacts.filter(c=>c.manager).length;
console.log(JSON.stringify({meetingCount: summary.meetingCount, contactCount: summary.contactCount, contacts_tenant_gal: gal, contacts_invite_fallback: fb, contacts_with_manager: withMgr, notes: result.notes.length, sha256: sha}));
