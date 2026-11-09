package main

import (
	"context"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

type fakePeer struct {
	calls int
	count int
	chat  string
}

func (f *fakePeer) BuildHistorySyncRequest(info *types.MessageInfo, count int) *waE2E.Message {
	f.count = count
	if info == nil || info.ID == "" {
		return nil
	}
	f.chat = info.Chat.String()
	return &waE2E.Message{}
}

func (f *fakePeer) SendPeerMessage(ctx context.Context, message *waE2E.Message) (whatsmeow.SendResponse, error) {
	f.calls++
	return whatsmeow.SendResponse{}, nil
}

func TestHistoryPlanRequiresAnchorAndCapsCount(t *testing.T) {
	empty := PlanHistoryBackfill("999888777666@lid", 0, nil, 50)
	if empty.CanRequest {
		t.Fatal("empty chat can be requested")
	}
	if empty.BlockReason == "" || empty.Coverage == "" {
		t.Fatal("missing limitation text")
	}

	anchor := &HistoryAnchor{ID: "abc", Timestamp: time.Unix(1_700_000_000, 0), IsFromMe: false}
	plan := PlanHistoryBackfill("15550001111@s.whatsapp.net", 18, anchor, 100)
	if !plan.CanRequest || plan.RequestCount != 50 {
		t.Fatalf("plan = %#v", plan)
	}
	if plan.StoredCount != 18 || plan.AnchorID != "abc" || plan.AnchorFromMe {
		t.Fatalf("anchor not preserved: %#v", plan)
	}
	def := PlanHistoryBackfill("120363000000@g.us", 1, anchor, 0)
	if !def.CanRequest || def.RequestCount != 50 {
		t.Fatalf("default count = %#v", def)
	}
}

func TestHistoryExecuteDryRunDoesNotSend(t *testing.T) {
	anchor := &HistoryAnchor{ID: "abc", Timestamp: time.Unix(1_700_000_000, 0), IsFromMe: true}
	plan := PlanHistoryBackfill("999888777666@lid", 18, anchor, 50)
	plan.DryRun = true
	peer := &fakePeer{}
	if err := ExecuteHistoryBackfill(context.Background(), peer, plan); err == nil {
		t.Fatal("dry run returned nil")
	}
	if peer.calls != 0 {
		t.Fatalf("dry run sent %d peer requests", peer.calls)
	}

	plan.DryRun = false
	if err := ExecuteHistoryBackfill(context.Background(), peer, plan); err != nil {
		t.Fatal(err)
	}
	if peer.calls != 1 || peer.count != 50 || peer.chat != "999888777666@lid" {
		t.Fatalf("peer = %#v", peer)
	}

	blocked := PlanHistoryBackfill("999888777666@lid", 0, nil, 50)
	blocked.DryRun = false
	if err := ExecuteHistoryBackfill(context.Background(), peer, blocked); err == nil {
		t.Fatal("missing anchor was sent")
	}
	if peer.calls != 1 {
		t.Fatalf("blocked plan sent again: %d", peer.calls)
	}
}

func TestHistoryDryRunDefaultsOn(t *testing.T) {
	if !historyDryRun(nil) {
		t.Fatal("omitted dry_run must default to true")
	}
	f := false
	if historyDryRun(&f) {
		t.Fatal("explicit false was treated as dry run")
	}
}

func TestHistoryAnchorSkipsEmptyIDsAndTies(t *testing.T) {
	store := testStore(t)
	chat := "15550001111@s.whatsapp.net"
	base := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	if err := store.StoreChat(chat, "15550001111", base); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("", chat, chat, "skip", base, false, "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("b", chat, chat, "second", base, false, "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("a", chat, chat, "first", base, true, "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("c", chat, chat, "later", base.Add(time.Minute), false, "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	count, anchor, err := store.HistoryAnchor(chat)
	if err != nil {
		t.Fatal(err)
	}
	if count != 4 {
		t.Fatalf("count = %d", count)
	}
	if anchor == nil || anchor.ID != "a" || !anchor.IsFromMe {
		t.Fatalf("anchor = %#v", anchor)
	}
}
