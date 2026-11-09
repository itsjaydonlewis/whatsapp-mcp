package main

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
)

type fakeContacts struct {
	items map[types.JID]types.ContactInfo
}

func (f fakeContacts) GetContact(ctx context.Context, user types.JID) (types.ContactInfo, error) {
	if f.items == nil {
		return types.ContactInfo{}, nil
	}
	info, ok := f.items[user.ToNonAD()]
	if !ok {
		return types.ContactInfo{}, nil
	}
	info.Found = true
	return info, nil
}

func (f fakeContacts) GetAllContacts(ctx context.Context) (map[types.JID]types.ContactInfo, error) {
	if f.items == nil {
		return map[types.JID]types.ContactInfo{}, nil
	}
	return f.items, nil
}

type fakeLIDs struct {
	pnToLID map[string]string
	lidToPN map[string]string
}

func (f fakeLIDs) GetLIDForPN(ctx context.Context, pn types.JID) (types.JID, error) {
	if pn.Server != types.DefaultUserServer {
		return types.JID{}, nil
	}
	user := f.pnToLID[pn.User]
	if user == "" {
		return types.JID{}, nil
	}
	return types.NewJID(user, types.HiddenUserServer), nil
}

func (f fakeLIDs) GetPNForLID(ctx context.Context, lid types.JID) (types.JID, error) {
	if lid.Server != types.HiddenUserServer {
		return types.JID{}, nil
	}
	user := f.lidToPN[lid.User]
	if user == "" {
		return types.JID{}, nil
	}
	return types.NewJID(user, types.DefaultUserServer), nil
}

func (f fakeLIDs) GetManyLIDsForPNs(ctx context.Context, pns []types.JID) (map[types.JID]types.JID, error) {
	out := map[types.JID]types.JID{}
	for _, pn := range pns {
		lid, _ := f.GetLIDForPN(ctx, pn)
		if !lid.IsEmpty() {
			out[pn.ToNonAD()] = lid
		}
	}
	return out, nil
}

func testStore(t *testing.T) *MessageStore {
	t.Helper()
	store, err := NewMessageStoreAt(filepath.Join(t.TempDir(), "messages.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	return store
}

func TestCanonicalizeJIDStripsDevice(t *testing.T) {
	if got := canonicalizeJIDString("15550001111:1@s.whatsapp.net"); got != "15550001111@s.whatsapp.net" {
		t.Fatalf("phone device suffix: %s", got)
	}
	if got := canonicalizeJIDString("999888777666:3@lid"); got != "999888777666@lid" {
		t.Fatalf("lid device suffix: %s", got)
	}
	if classifyJID("120363000000@g.us") != kindGroup {
		t.Fatal("group classification")
	}
	if classifyJID("15550001111@s.whatsapp.net") != kindPhone {
		t.Fatal("phone classification")
	}
	if classifyJID("999888777666@lid") != kindLID {
		t.Fatal("lid classification")
	}
}

func TestMappingDirectionAndConflicts(t *testing.T) {
	lid := "999888777666@lid"
	pn := "15550001111@s.whatsapp.net"
	other := "15550002222@s.whatsapp.net"
	result := CanonicalizeMappings([]LIDPair{
		{LID: "999888777666:4@lid", PN: "15550001111:2@s.whatsapp.net"},
		{LID: lid, PN: pn},
	})
	if len(result.Accepted) != 1 || result.Accepted[0].LID != lid || result.Accepted[0].PN != pn {
		t.Fatalf("expected one canonical pair, got %#v", result.Accepted)
	}
	if len(result.Conflicts) != 0 {
		t.Fatalf("duplicate direction is not a conflict: %#v", result.Conflicts)
	}

	conflicted := CanonicalizeMappings([]LIDPair{
		{LID: lid, PN: pn},
		{LID: lid, PN: other},
	})
	if len(conflicted.Accepted) != 0 {
		t.Fatalf("lid mapped to two phones was accepted: %#v", conflicted.Accepted)
	}
	if len(conflicted.Conflicts) == 0 {
		t.Fatal("expected conflicts")
	}

	reverse := CanonicalizeMappings([]LIDPair{
		{LID: lid, PN: pn},
		{LID: "111222333444@lid", PN: pn},
	})
	if len(reverse.Accepted) != 0 {
		t.Fatalf("one phone mapped to two lids was accepted: %#v", reverse.Accepted)
	}

	group := CanonicalizeMappings([]LIDPair{{LID: "120363000000@g.us", PN: pn}})
	if len(group.Accepted) != 0 || len(group.Conflicts) != 0 {
		t.Fatalf("group jid must not become a person: %#v", group)
	}
}

func TestRefreshRepairsRawNamesWithoutMergingSimilarNames(t *testing.T) {
	store := testStore(t)
	pn := types.NewJID("15550001111", types.DefaultUserServer)
	lid := types.NewJID("999888777666", types.HiddenUserServer)
	other := types.NewJID("15550002222", types.DefaultUserServer)
	redacted := types.NewJID("111222333444", types.HiddenUserServer)
	group := "120363000000@g.us"
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)

	if err := store.StoreChat(lid.String(), lid.User, now); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreChat(pn.String(), "Nickname", now); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreChat(other.String(), other.User, now); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreChat(redacted.String(), redacted.User, now); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreChat(group, "Book Club", now); err != nil {
		t.Fatal(err)
	}

	contacts := fakeContacts{items: map[types.JID]types.ContactInfo{
		pn:       {Found: true, FullName: "Ada Example", PushName: "Ada Push"},
		lid:      {Found: true, PushName: "Ada Push"},
		other:    {Found: true, FullName: "Ada Examples"},
		redacted: {Found: true, RedactedPhone: "+1∙∙∙∙∙∙∙∙80"},
	}}
	lids := fakeLIDs{
		pnToLID: map[string]string{pn.User: lid.User},
		lidToPN: map[string]string{lid.User: pn.User},
	}
	summary, err := RefreshIdentityCache(context.Background(), contacts, lids, store, nil)
	if err != nil {
		t.Fatal(err)
	}
	if summary.Mappings != 1 {
		t.Fatalf("mappings = %d", summary.Mappings)
	}
	if summary.ChatsRenamed != 2 {
		t.Fatalf("renamed = %d, want the raw lid chat and the raw other phone chat", summary.ChatsRenamed)
	}

	var lidName, pnName, otherName, redactedName, groupName string
	if err := store.db.QueryRow(`SELECT name FROM chats WHERE jid = ?`, lid.String()).Scan(&lidName); err != nil {
		t.Fatal(err)
	}
	if err := store.db.QueryRow(`SELECT name FROM chats WHERE jid = ?`, pn.String()).Scan(&pnName); err != nil {
		t.Fatal(err)
	}
	if err := store.db.QueryRow(`SELECT name FROM chats WHERE jid = ?`, other.String()).Scan(&otherName); err != nil {
		t.Fatal(err)
	}
	if err := store.db.QueryRow(`SELECT name FROM chats WHERE jid = ?`, redacted.String()).Scan(&redactedName); err != nil {
		t.Fatal(err)
	}
	if err := store.db.QueryRow(`SELECT name FROM chats WHERE jid = ?`, group).Scan(&groupName); err != nil {
		t.Fatal(err)
	}
	if lidName != "Ada Example" {
		t.Fatalf("lid chat name = %q", lidName)
	}
	if pnName != "Nickname" {
		t.Fatalf("custom name was replaced: %q", pnName)
	}
	if otherName != "Ada Examples" {
		t.Fatalf("own full name was not applied, or names were merged: %q", otherName)
	}
	if redactedName != redacted.User {
		t.Fatalf("redacted phone was used as a name: %q", redactedName)
	}
	if groupName != "Book Club" {
		t.Fatalf("group name changed: %q", groupName)
	}

	var mappedPN string
	if err := store.db.QueryRow(`SELECT pn FROM lid_map WHERE lid = ?`, lid.String()).Scan(&mappedPN); err != nil {
		t.Fatal(err)
	}
	if mappedPN != pn.String() {
		t.Fatalf("lid map direction = %s", mappedPN)
	}
	var phoneCount int
	if err := store.db.QueryRow(`SELECT COUNT(*) FROM contact_cache WHERE business_name LIKE '%∙%' OR full_name LIKE '%∙%'`).Scan(&phoneCount); err != nil {
		t.Fatal(err)
	}
	if phoneCount != 0 {
		t.Fatal("redacted phone was stored")
	}
}

func TestConflictingExtraPairsAreNotStored(t *testing.T) {
	store := testStore(t)
	lid := "999888777666@lid"
	summary, err := RefreshIdentityCache(context.Background(), fakeContacts{}, fakeLIDs{}, store, []LIDPair{
		{LID: lid, PN: "15550001111@s.whatsapp.net", Source: "history_conversation"},
		{LID: lid, PN: "15550002222@s.whatsapp.net", Source: "history_conversation"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if summary.Mappings != 0 || summary.Conflicts == 0 {
		t.Fatalf("summary = %#v", summary)
	}
	linked, err := store.SaveLIDPair(LIDPair{LID: lid, PN: "15550001111@s.whatsapp.net"}, "message_alt")
	if err != nil {
		t.Fatal(err)
	}
	if linked {
		t.Fatal("conflicted lid was linked by a later pair")
	}
}

func TestIsRawChatNameUsesAliasesOnly(t *testing.T) {
	lid := "999888777666@lid"
	pn := "15550001111@s.whatsapp.net"
	if !IsRawChatName("15550001111", lid, []string{lid, pn}) {
		t.Fatal("linked phone digits should count as a raw label on the lid chat")
	}
	if IsRawChatName("Ada Example", lid, []string{lid, pn}) {
		t.Fatal("real name was treated as raw")
	}
	if IsRawChatName("Book Club", "120363000000@g.us", nil) {
		t.Fatal("group title was treated as raw")
	}
	if !IsRawChatName("Group 120363000000", "120363000000@g.us", nil) {
		t.Fatal("group placeholder should be raw")
	}
}
