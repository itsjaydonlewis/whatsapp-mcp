package main

import (
	"context"
	"database/sql"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"
)

const (
	kindPhone   = "phone"
	kindLID     = "lid"
	kindGroup   = "group"
	kindOther   = "other"
	kindUnknown = "unknown"
)

// Name priority for one verified identity, phone record first then LID:
// full_name, business_name, push_name, first_name.
// Unlinked and conflicting JIDs use only their own record.
// Redacted phones are never names and never match keys.
// A stored chat name that is not a raw identifier is kept.

type storedContact struct {
	JID          string
	FullName     string
	FirstName    string
	PushName     string
	BusinessName string
}

// LIDPair is an explicit phone-JID <-> LID link. It is never inferred from names.
type LIDPair struct {
	LID    string
	PN     string
	Source string
}

// MappingConflict records contradictory explicit maps that must not link identities.
type MappingConflict struct {
	LID    string
	PN     string
	Reason string
}

// MappingResult is the accepted 1:1 links and the rejected contradictions.
type MappingResult struct {
	Accepted  []LIDPair
	Conflicts []MappingConflict
}

// RefreshSummary is counts only. It intentionally contains no contact names.
type RefreshSummary struct {
	Contacts     int `json:"contacts"`
	Mappings     int `json:"mappings"`
	Conflicts    int `json:"conflicts"`
	ChatsRenamed int `json:"chats_renamed"`
}

// IdentityStatus is a name-free view of the local identity cache.
type IdentityStatus struct {
	Contacts  int `json:"contacts"`
	Mappings  int `json:"mappings"`
	Conflicts int `json:"conflicts"`
}

// ContactSource is the documented whatsmeow contact store surface this repair reads.
type ContactSource interface {
	GetContact(ctx context.Context, user types.JID) (types.ContactInfo, error)
	GetAllContacts(ctx context.Context) (map[types.JID]types.ContactInfo, error)
}

// LIDSource is the documented whatsmeow LID mapping surface this repair reads.
type LIDSource interface {
	GetPNForLID(ctx context.Context, lid types.JID) (types.JID, error)
	GetLIDForPN(ctx context.Context, pn types.JID) (types.JID, error)
	GetManyLIDsForPNs(ctx context.Context, pns []types.JID) (map[types.JID]types.JID, error)
}

var identityWriteMu sync.Mutex

func canonicalizeJIDString(value string) string {
	value = strings.TrimSpace(value)
	if value == "" || !strings.Contains(value, "@") {
		return value
	}
	jid, err := types.ParseJID(value)
	if err != nil || jid.Server == "" || jid.User == "" {
		return value
	}
	return jid.ToNonAD().String()
}

func userPart(jid string) string {
	canon := canonicalizeJIDString(jid)
	if i := strings.Index(canon, "@"); i >= 0 {
		return canon[:i]
	}
	return canon
}

func classifyJID(jid string) string {
	canon := canonicalizeJIDString(jid)
	if !strings.Contains(canon, "@") {
		return kindUnknown
	}
	switch canon[strings.Index(canon, "@")+1:] {
	case types.DefaultUserServer:
		return kindPhone
	case types.HiddenUserServer:
		return kindLID
	case types.GroupServer:
		return kindGroup
	default:
		return kindOther
	}
}

func IsRawChatName(name, jid string, aliases []string) bool {
	n := strings.TrimSpace(name)
	if n == "" {
		return true
	}
	candidates := make([]string, 0, len(aliases)+1)
	if strings.TrimSpace(jid) != "" {
		candidates = append(candidates, jid)
	}
	candidates = append(candidates, aliases...)
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		canon := canonicalizeJIDString(candidate)
		user := userPart(canon)
		if n == candidate || n == canon || (user != "" && n == user) {
			return true
		}
		if classifyJID(canon) == kindGroup && n == "Group "+user {
			return true
		}
	}
	return false
}

func firstNamed(records []*storedContact) (string, string) {
	type field struct {
		get    func(*storedContact) string
		source string
	}
	fields := []field{
		{func(c *storedContact) string { return c.FullName }, "full_name"},
		{func(c *storedContact) string { return c.BusinessName }, "business_name"},
		{func(c *storedContact) string { return c.PushName }, "push_name"},
		{func(c *storedContact) string { return c.FirstName }, "first_name"},
	}
	for _, f := range fields {
		for _, rec := range records {
			if rec == nil {
				continue
			}
			if v := strings.TrimSpace(f.get(rec)); v != "" {
				return v, f.source
			}
		}
	}
	return "", ""
}

// CanonicalizeMappings keeps one-to-one explicit links and drops contradictions.
// Group JIDs and incomplete pairs are ignored. Name similarity is never consulted.
func CanonicalizeMappings(pairs []LIDPair) MappingResult {
	type norm struct {
		lid string
		pn  string
	}
	norms := make([]norm, 0, len(pairs))
	for _, pair := range pairs {
		lid := canonicalizeJIDString(pair.LID)
		pn := canonicalizeJIDString(pair.PN)
		if classifyJID(lid) != kindLID || classifyJID(pn) != kindPhone {
			continue
		}
		norms = append(norms, norm{lid: lid, pn: pn})
	}

	lidPNs := map[string]map[string]struct{}{}
	pnLIDs := map[string]map[string]struct{}{}
	for _, n := range norms {
		if lidPNs[n.lid] == nil {
			lidPNs[n.lid] = map[string]struct{}{}
		}
		lidPNs[n.lid][n.pn] = struct{}{}
		if pnLIDs[n.pn] == nil {
			pnLIDs[n.pn] = map[string]struct{}{}
		}
		pnLIDs[n.pn][n.lid] = struct{}{}
	}

	badLID := map[string]bool{}
	badPN := map[string]bool{}
	conflicts := make([]MappingConflict, 0)
	for lid, pns := range lidPNs {
		if len(pns) <= 1 {
			continue
		}
		badLID[lid] = true
		for pn := range pns {
			conflicts = append(conflicts, MappingConflict{LID: lid, PN: pn, Reason: "lid_maps_to_multiple_phone_jids"})
		}
	}
	for pn, lids := range pnLIDs {
		if len(lids) <= 1 {
			continue
		}
		badPN[pn] = true
		for lid := range lids {
			conflicts = append(conflicts, MappingConflict{LID: lid, PN: pn, Reason: "phone_jid_maps_to_multiple_lids"})
		}
	}

	seen := map[string]struct{}{}
	accepted := make([]LIDPair, 0)
	for _, n := range norms {
		if badLID[n.lid] || badPN[n.pn] {
			continue
		}
		key := n.lid + "|" + n.pn
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		accepted = append(accepted, LIDPair{LID: n.lid, PN: n.pn})
	}
	sort.Slice(accepted, func(i, j int) bool {
		if accepted[i].LID == accepted[j].LID {
			return accepted[i].PN < accepted[j].PN
		}
		return accepted[i].LID < accepted[j].LID
	})
	sort.Slice(conflicts, func(i, j int) bool {
		if conflicts[i].LID == conflicts[j].LID {
			if conflicts[i].PN == conflicts[j].PN {
				return conflicts[i].Reason < conflicts[j].Reason
			}
			return conflicts[i].PN < conflicts[j].PN
		}
		return conflicts[i].LID < conflicts[j].LID
	})
	return MappingResult{Accepted: accepted, Conflicts: dedupeConflicts(conflicts)}
}

func dedupeConflicts(in []MappingConflict) []MappingConflict {
	seen := map[string]struct{}{}
	out := make([]MappingConflict, 0, len(in))
	for _, c := range in {
		key := c.LID + "|" + c.PN + "|" + c.Reason
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		out = append(out, c)
	}
	return out
}

func contactFromInfo(jid types.JID, info types.ContactInfo) *storedContact {
	c := &storedContact{
		JID:          jid.ToNonAD().String(),
		FullName:     strings.TrimSpace(info.FullName),
		FirstName:    strings.TrimSpace(info.FirstName),
		PushName:     strings.TrimSpace(info.PushName),
		BusinessName: strings.TrimSpace(info.BusinessName),
	}
	if c.JID == "" || (c.FullName == "" && c.FirstName == "" && c.PushName == "" && c.BusinessName == "") {
		return nil
	}
	return c
}

func ensureIdentitySchema(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS contact_cache (
			jid TEXT PRIMARY KEY,
			full_name TEXT NOT NULL DEFAULT '',
			first_name TEXT NOT NULL DEFAULT '',
			push_name TEXT NOT NULL DEFAULT '',
			business_name TEXT NOT NULL DEFAULT ''
		);
		CREATE TABLE IF NOT EXISTS lid_map (
			lid TEXT PRIMARY KEY,
			pn TEXT NOT NULL UNIQUE,
			source TEXT NOT NULL DEFAULT ''
		);
		CREATE TABLE IF NOT EXISTS identity_conflict (
			lid TEXT NOT NULL,
			pn TEXT NOT NULL,
			reason TEXT NOT NULL,
			PRIMARY KEY (lid, pn, reason)
		);
		CREATE TABLE IF NOT EXISTS chat_history_meta (
			chat_jid TEXT PRIMARY KEY,
			sync_type TEXT NOT NULL DEFAULT '',
			end_of_history_transfer INTEGER,
			end_of_history_transfer_type TEXT NOT NULL DEFAULT '',
			updated_at TEXT NOT NULL DEFAULT ''
		);
		CREATE INDEX IF NOT EXISTS idx_messages_chat_time_id ON messages(chat_jid, timestamp, id);
	`)
	return err
}

func NewMessageStoreAt(dbPath string) (*MessageStore, error) {
	db, err := sql.Open("sqlite3", "file:"+dbPath+"?_foreign_keys=on")
	if err != nil {
		return nil, fmt.Errorf("failed to open message database: %v", err)
	}
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS chats (
			jid TEXT PRIMARY KEY,
			name TEXT,
			last_message_time TIMESTAMP
		);
		CREATE TABLE IF NOT EXISTS messages (
			id TEXT,
			chat_jid TEXT,
			sender TEXT,
			content TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN,
			media_type TEXT,
			filename TEXT,
			url TEXT,
			media_key BLOB,
			file_sha256 BLOB,
			file_enc_sha256 BLOB,
			file_length INTEGER,
			PRIMARY KEY (id, chat_jid),
			FOREIGN KEY (chat_jid) REFERENCES chats(jid)
		);
	`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create tables: %v", err)
	}
	if err := ensureIdentitySchema(db); err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create identity tables: %v", err)
	}
	return &MessageStore{db: db}, nil
}

func (store *MessageStore) IdentityCacheStatus() (IdentityStatus, error) {
	var status IdentityStatus
	if err := store.db.QueryRow(`SELECT COUNT(*) FROM contact_cache`).Scan(&status.Contacts); err != nil {
		return status, err
	}
	if err := store.db.QueryRow(`SELECT COUNT(*) FROM lid_map`).Scan(&status.Mappings); err != nil {
		return status, err
	}
	if err := store.db.QueryRow(`SELECT COUNT(*) FROM identity_conflict`).Scan(&status.Conflicts); err != nil {
		return status, err
	}
	return status, nil
}

func (store *MessageStore) loadLIDPairs() ([]LIDPair, error) {
	rows, err := store.db.Query(`SELECT lid, pn, source FROM lid_map`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var pairs []LIDPair
	for rows.Next() {
		var pair LIDPair
		if err := rows.Scan(&pair.LID, &pair.PN, &pair.Source); err != nil {
			return nil, err
		}
		pairs = append(pairs, pair)
	}
	return pairs, rows.Err()
}

func (store *MessageStore) replaceIdentityCache(contacts []storedContact, result MappingResult, sources map[string]string) (int, error) {
	tx, err := store.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	if _, err := tx.Exec(`DELETE FROM contact_cache`); err != nil {
		return 0, err
	}
	if _, err := tx.Exec(`DELETE FROM lid_map`); err != nil {
		return 0, err
	}
	if _, err := tx.Exec(`DELETE FROM identity_conflict`); err != nil {
		return 0, err
	}
	for _, contact := range contacts {
		if _, err := tx.Exec(
			`INSERT INTO contact_cache (jid, full_name, first_name, push_name, business_name) VALUES (?, ?, ?, ?, ?)`,
			contact.JID, contact.FullName, contact.FirstName, contact.PushName, contact.BusinessName,
		); err != nil {
			return 0, err
		}
	}
	for _, pair := range result.Accepted {
		source := sources[pair.LID+"|"+pair.PN]
		if source == "" {
			source = "store"
		}
		if _, err := tx.Exec(`INSERT INTO lid_map (lid, pn, source) VALUES (?, ?, ?)`, pair.LID, pair.PN, source); err != nil {
			return 0, err
		}
	}
	for _, conflict := range result.Conflicts {
		if _, err := tx.Exec(
			`INSERT INTO identity_conflict (lid, pn, reason) VALUES (?, ?, ?)`,
			conflict.LID, conflict.PN, conflict.Reason,
		); err != nil {
			return 0, err
		}
	}
	renamed, err := repairRawChatNamesTx(tx, contacts, result)
	if err != nil {
		return 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return renamed, nil
}

func repairRawChatNamesTx(tx *sql.Tx, contacts []storedContact, result MappingResult) (int, error) {
	byJID := map[string]*storedContact{}
	for i := range contacts {
		c := contacts[i]
		byJID[c.JID] = &c
	}
	lidToPN := map[string]string{}
	pnToLID := map[string]string{}
	for _, pair := range result.Accepted {
		lidToPN[pair.LID] = pair.PN
		pnToLID[pair.PN] = pair.LID
	}
	conflict := map[string]bool{}
	for _, c := range result.Conflicts {
		conflict[c.LID] = true
		conflict[c.PN] = true
	}

	rows, err := tx.Query(`SELECT jid, name FROM chats`)
	if err != nil {
		return 0, err
	}
	defer rows.Close()

	type update struct{ jid, name string }
	var updates []update
	for rows.Next() {
		var jid, name string
		if err := rows.Scan(&jid, &name); err != nil {
			return 0, err
		}
		jid = canonicalizeJIDString(jid)
		if classifyJID(jid) == kindGroup || classifyJID(jid) == kindOther {
			continue
		}
		resolved, aliases := resolveCached(jid, byJID, lidToPN, pnToLID, conflict)
		if resolved == "" || IsRawChatName(resolved, jid, aliases) {
			continue
		}
		if !IsRawChatName(name, jid, aliases) {
			continue
		}
		if strings.TrimSpace(name) == resolved {
			continue
		}
		updates = append(updates, update{jid: jid, name: resolved})
	}
	if err := rows.Err(); err != nil {
		return 0, err
	}
	for _, upd := range updates {
		if _, err := tx.Exec(`UPDATE chats SET name = ? WHERE jid = ?`, upd.name, upd.jid); err != nil {
			return 0, err
		}
	}
	return len(updates), nil
}

func resolveCached(jid string, byJID map[string]*storedContact, lidToPN, pnToLID map[string]string, conflict map[string]bool) (string, []string) {
	jid = canonicalizeJIDString(jid)
	aliases := []string{jid}
	if conflict[jid] {
		name, _ := firstNamed([]*storedContact{byJID[jid]})
		return name, aliases
	}
	var peer string
	switch classifyJID(jid) {
	case kindLID:
		peer = lidToPN[jid]
	case kindPhone:
		peer = pnToLID[jid]
	}
	records := []*storedContact{byJID[jid]}
	if peer != "" && !conflict[peer] {
		aliases = append(aliases, peer)
		peerRec := byJID[peer]
		if classifyJID(jid) == kindPhone {
			records = []*storedContact{byJID[jid], peerRec}
		} else {
			records = []*storedContact{peerRec, byJID[jid]}
		}
	}
	name, _ := firstNamed(records)
	return name, aliases
}

func (store *MessageStore) distinctUserJIDs() ([]types.JID, error) {
	rows, err := store.db.Query(`SELECT jid FROM chats UNION SELECT sender FROM messages`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []types.JID
	seen := map[string]struct{}{}
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		canon := canonicalizeJIDString(raw)
		kind := classifyJID(canon)
		if kind != kindPhone && kind != kindLID {
			continue
		}
		if _, ok := seen[canon]; ok {
			continue
		}
		seen[canon] = struct{}{}
		jid, err := types.ParseJID(canon)
		if err != nil {
			continue
		}
		out = append(out, jid.ToNonAD())
	}
	return out, rows.Err()
}

func consistentAlt(ctx context.Context, lids LIDSource, jid types.JID) (types.JID, bool) {
	jid = jid.ToNonAD()
	var alt types.JID
	var err error
	switch jid.Server {
	case types.DefaultUserServer:
		alt, err = lids.GetLIDForPN(ctx, jid)
	case types.HiddenUserServer:
		alt, err = lids.GetPNForLID(ctx, jid)
	default:
		return types.EmptyJID, false
	}
	if err != nil || alt.IsEmpty() {
		return types.EmptyJID, false
	}
	alt = alt.ToNonAD()
	var back types.JID
	switch alt.Server {
	case types.HiddenUserServer:
		back, err = lids.GetPNForLID(ctx, alt)
	case types.DefaultUserServer:
		back, err = lids.GetLIDForPN(ctx, alt)
	default:
		return types.EmptyJID, false
	}
	if err != nil || back.IsEmpty() || back.ToNonAD().String() != jid.String() {
		return types.EmptyJID, false
	}
	return alt, false
}

// RefreshIdentityCache reads the documented contact and LID stores and rewrites
// only the local identity cache plus raw chat labels. It does not send messages
// or modify the whatsmeow session database.
func RefreshIdentityCache(ctx context.Context, contacts ContactSource, lids LIDSource, store *MessageStore, extra []LIDPair) (RefreshSummary, error) {
	var summary RefreshSummary
	if contacts == nil || lids == nil || store == nil {
		return summary, fmt.Errorf("identity refresh requires contact store, lid store, and message store")
	}
	all, err := contacts.GetAllContacts(ctx)
	if err != nil {
		return summary, err
	}

	contactRows := make([]storedContact, 0, len(all))
	jidSet := map[string]types.JID{}
	for jid, info := range all {
		jid = jid.ToNonAD()
		kind := classifyJID(jid.String())
		if kind != kindPhone && kind != kindLID {
			continue
		}
		jidSet[jid.String()] = jid
		if row := contactFromInfo(jid, info); row != nil {
			contactRows = append(contactRows, *row)
		}
	}
	known, err := store.distinctUserJIDs()
	if err != nil {
		return summary, err
	}
	for _, jid := range known {
		jidSet[jid.String()] = jid
	}

	var pnList []types.JID
	var lidList []types.JID
	for _, jid := range jidSet {
		switch jid.Server {
		case types.DefaultUserServer:
			pnList = append(pnList, jid)
		case types.HiddenUserServer:
			lidList = append(lidList, jid)
		}
	}

	pairs := make([]LIDPair, 0)
	sources := map[string]string{}
	add := func(lid, pn types.JID, source string) {
		if lid.IsEmpty() || pn.IsEmpty() {
			return
		}
		lid = lid.ToNonAD()
		pn = pn.ToNonAD()
		if lid.Server != types.HiddenUserServer || pn.Server != types.DefaultUserServer || lid.User == "" || pn.User == "" {
			return
		}
		key := lid.String() + "|" + pn.String()
		if _, ok := sources[key]; !ok {
			sources[key] = source
		}
		pairs = append(pairs, LIDPair{LID: lid.String(), PN: pn.String(), Source: source})
	}

	if len(pnList) > 0 {
		many, err := lids.GetManyLIDsForPNs(ctx, pnList)
		if err != nil {
			return summary, err
		}
		for pn, lid := range many {
			pn = pn.ToNonAD()
			lid = lid.ToNonAD()
			if lid.IsEmpty() || lid.Server != types.HiddenUserServer || pn.Server != types.DefaultUserServer {
				continue
			}
			back, err := lids.GetPNForLID(ctx, lid)
			if err != nil || back.IsEmpty() || back.ToNonAD().String() != pn.String() {
				continue
			}
			add(lid, pn, "store")
		}
	}
	for _, lid := range lidList {
		if pn, _ := consistentAlt(ctx, lids, lid); !pn.IsEmpty() {
			add(lid, pn, "store")
		}
	}
	for _, pair := range extra {
		lid, err1 := types.ParseJID(canonicalizeJIDString(pair.LID))
		pn, err2 := types.ParseJID(canonicalizeJIDString(pair.PN))
		if err1 != nil || err2 != nil {
			continue
		}
		add(lid, pn, pair.Source)
	}

	result := CanonicalizeMappings(pairs)
	for i := range result.Accepted {
		key := result.Accepted[i].LID + "|" + result.Accepted[i].PN
		if sources[key] != "" {
			result.Accepted[i].Source = sources[key]
		}
	}

	identityWriteMu.Lock()
	defer identityWriteMu.Unlock()
	renamed, err := store.replaceIdentityCache(contactRows, result, sources)
	if err != nil {
		return summary, err
	}
	summary.Contacts = len(contactRows)
	summary.Mappings = len(result.Accepted)
	summary.Conflicts = len(result.Conflicts)
	summary.ChatsRenamed = renamed
	return summary, nil
}

// SaveLIDPair records one explicit link. A contradiction removes the link
// instead of choosing a side.
func (store *MessageStore) SaveLIDPair(pair LIDPair, source string) (bool, error) {
	pair.LID = canonicalizeJIDString(pair.LID)
	pair.PN = canonicalizeJIDString(pair.PN)
	pair.Source = source
	if classifyJID(pair.LID) != kindLID || classifyJID(pair.PN) != kindPhone {
		return false, nil
	}
	identityWriteMu.Lock()
	defer identityWriteMu.Unlock()

	blocked, err := store.jidInConflict(pair.LID)
	if err != nil {
		return false, err
	}
	if !blocked {
		blocked, err = store.jidInConflict(pair.PN)
		if err != nil {
			return false, err
		}
	}
	if blocked {
		return false, nil
	}
	existing, err := store.loadLIDPairs()
	if err != nil {
		return false, err
	}
	combined := append(existing, pair)
	result := CanonicalizeMappings(combined)
	linked := false
	for _, accepted := range result.Accepted {
		if accepted.LID == pair.LID && accepted.PN == pair.PN {
			linked = true
		}
	}
	sources := map[string]string{}
	for _, item := range existing {
		sources[item.LID+"|"+item.PN] = item.Source
	}
	if linked {
		sources[pair.LID+"|"+pair.PN] = source
	}
	contacts, err := store.loadContacts()
	if err != nil {
		return false, err
	}
	if _, err := store.replaceIdentityCache(contacts, result, sources); err != nil {
		return false, err
	}
	return linked, nil
}

func (store *MessageStore) jidInConflict(jid string) (bool, error) {
	var n int
	err := store.db.QueryRow(`SELECT COUNT(*) FROM identity_conflict WHERE lid = ? OR pn = ?`, jid, jid).Scan(&n)
	return n > 0, err
}

func (store *MessageStore) cachedResolution(jid string) (string, []string) {
	contacts, err := store.loadContacts()
	if err != nil {
		return "", []string{canonicalizeJIDString(jid)}
	}
	pairs, err := store.loadLIDPairs()
	if err != nil {
		return "", []string{canonicalizeJIDString(jid)}
	}
	result := CanonicalizeMappings(pairs)
	by := map[string]*storedContact{}
	for i := range contacts {
		c := contacts[i]
		by[c.JID] = &c
	}
	lidToPN := map[string]string{}
	pnToLID := map[string]string{}
	for _, pair := range result.Accepted {
		lidToPN[pair.LID] = pair.PN
		pnToLID[pair.PN] = pair.LID
	}
	conflict := map[string]bool{}
	for _, c := range result.Conflicts {
		conflict[c.LID] = true
		conflict[c.PN] = true
	}
	rows, err := store.db.Query(`SELECT lid, pn FROM identity_conflict`)
	if err == nil {
		defer rows.Close()
		for rows.Next() {
			var lid, pn string
			if scanErr := rows.Scan(&lid, &pn); scanErr == nil {
				conflict[lid] = true
				conflict[pn] = true
			}
		}
	}
	return resolveCached(jid, by, lidToPN, pnToLID, conflict)
}

func (store *MessageStore) loadContacts() ([]storedContact, error) {
	rows, err := store.db.Query(`SELECT jid, full_name, first_name, push_name, business_name FROM contact_cache`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []storedContact
	for rows.Next() {
		var c storedContact
		if err := rows.Scan(&c.JID, &c.FullName, &c.FirstName, &c.PushName, &c.BusinessName); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

func (store *MessageStore) UpsertContact(contact storedContact) error {
	contact.JID = canonicalizeJIDString(contact.JID)
	if classifyJID(contact.JID) != kindPhone && classifyJID(contact.JID) != kindLID {
		return nil
	}
	identityWriteMu.Lock()
	defer identityWriteMu.Unlock()
	_, err := store.db.Exec(`
		INSERT INTO contact_cache (jid, full_name, first_name, push_name, business_name)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(jid) DO UPDATE SET
			full_name=CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE contact_cache.full_name END,
			first_name=CASE WHEN excluded.first_name != '' THEN excluded.first_name ELSE contact_cache.first_name END,
			push_name=CASE WHEN excluded.push_name != '' THEN excluded.push_name ELSE contact_cache.push_name END,
			business_name=CASE WHEN excluded.business_name != '' THEN excluded.business_name ELSE contact_cache.business_name END
	`, contact.JID, contact.FullName, contact.FirstName, contact.PushName, contact.BusinessName)
	return err
}

// UpsertPushName records a push name from the documented PushName event.
// An empty value does not erase a name already stored.
func (store *MessageStore) UpsertPushName(jid types.JID, pushName string) error {
	pushName = strings.TrimSpace(pushName)
	if pushName == "" {
		return nil
	}
	return store.UpsertContact(storedContact{JID: jid.ToNonAD().String(), PushName: pushName})
}

func PairFromJIDs(a, b types.JID) (LIDPair, bool) {
	a = a.ToNonAD()
	b = b.ToNonAD()
	var lid, pn types.JID
	switch {
	case a.Server == types.HiddenUserServer && b.Server == types.DefaultUserServer:
		lid, pn = a, b
	case b.Server == types.HiddenUserServer && a.Server == types.DefaultUserServer:
		lid, pn = b, a
	default:
		return LIDPair{}, false
	}
	if lid.User == "" || pn.User == "" {
		return LIDPair{}, false
	}
	return LIDPair{LID: lid.String(), PN: pn.String()}, true
}

func HistoryPairFromRaw(lidRaw, pnRaw string) (LIDPair, bool) {
	lid := canonicalizeJIDString(strings.TrimSpace(lidRaw))
	pn := canonicalizeJIDString(strings.TrimSpace(pnRaw))
	if classifyJID(lid) != kindLID || classifyJID(pn) != kindPhone {
		return LIDPair{}, false
	}
	return LIDPair{LID: lid, PN: pn, Source: "history_conversation"}, true
}

func resolveDMName(ctx context.Context, contacts ContactSource, lids LIDSource, store *MessageStore, jid types.JID) (string, []string) {
	jid = jid.ToNonAD()
	aliases := []string{jid.String()}
	if jid.Server != types.DefaultUserServer && jid.Server != types.HiddenUserServer {
		return "", aliases
	}
	var own *storedContact
	if contacts != nil {
		info, err := contacts.GetContact(ctx, jid)
		if err == nil {
			own = contactFromInfo(jid, info)
			if own != nil && store != nil {
				_ = store.UpsertContact(*own)
			}
		}
	}
	var peer types.JID
	if lids != nil {
		peer, _ = consistentAlt(ctx, lids, jid)
	}
	records := []*storedContact{own}
	if !peer.IsEmpty() {
		aliases = append(aliases, peer.ToNonAD().String())
		var other *storedContact
		if contacts != nil {
			info, err := contacts.GetContact(ctx, peer)
			if err == nil {
				other = contactFromInfo(peer, info)
				if other != nil && store != nil {
					_ = store.UpsertContact(*other)
				}
			}
		}
		if store != nil {
			if pair, ok := PairFromJIDs(jid, peer); ok {
				_, _ = store.SaveLIDPair(pair, "store")
			}
		}
		if jid.Server == types.DefaultUserServer {
			records = []*storedContact{own, other}
		} else {
			records = []*storedContact{other, own}
		}
	}
	name, _ := firstNamed(records)
	if name == "" && store != nil {
		if cached, cachedAliases := store.cachedResolution(jid.String()); cached != "" {
			return cached, cachedAliases
		}
	}
	return name, aliases
}

var (
	refreshSchedMu sync.Mutex
	refreshFn      func()
	refreshRunning bool
	refreshAgain   bool
)

// SetIdentityRefresher installs the local-store refresh used after contact events.
func SetIdentityRefresher(fn func()) {
	refreshSchedMu.Lock()
	refreshFn = fn
	refreshSchedMu.Unlock()
}

// ScheduleIdentityRefresh coalesces local identity-cache rebuilds. It does not
// request WhatsApp history and it does not send chat messages.
func ScheduleIdentityRefresh() {
	refreshSchedMu.Lock()
	fn := refreshFn
	if fn == nil {
		refreshSchedMu.Unlock()
		return
	}
	if refreshRunning {
		refreshAgain = true
		refreshSchedMu.Unlock()
		return
	}
	refreshRunning = true
	refreshSchedMu.Unlock()
	go func() {
		for {
			fn()
			refreshSchedMu.Lock()
			if !refreshAgain {
				refreshRunning = false
				refreshSchedMu.Unlock()
				return
			}
			refreshAgain = false
			refreshSchedMu.Unlock()
		}
	}()
}

func (store *MessageStore) SaveChatHistoryMeta(chatJID, syncType, endType string, endFlag *bool) error {
	chatJID = canonicalizeJIDString(chatJID)
	var end any
	if endFlag != nil {
		if *endFlag {
			end = 1
		} else {
			end = 0
		}
	}
	_, err := store.db.Exec(`
		INSERT INTO chat_history_meta (chat_jid, sync_type, end_of_history_transfer, end_of_history_transfer_type, updated_at)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(chat_jid) DO UPDATE SET
			sync_type=excluded.sync_type,
			end_of_history_transfer=excluded.end_of_history_transfer,
			end_of_history_transfer_type=excluded.end_of_history_transfer_type,
			updated_at=excluded.updated_at
	`, chatJID, syncType, end, endType, time.Now().UTC().Format(time.RFC3339))
	return err
}
