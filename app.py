import os
import re
import uuid

import streamlit as st
from langgraph.types import Command

import gmail_imap
import whatsapp_store as wa_store
from agent import WHATSAPP_ENABLED, get_agent

WA_ON = os.getenv("WHATSAPP_ENABLED", "false").strip().lower() == "true"

st.set_page_config(
    page_title="Smartreply-ai" if WA_ON else "AI Email Assistant",
    page_icon="💬" if WA_ON else "✉️",
)
st.subheader("Smartreply-ai: Email + WhatsApp Assistant" if WA_ON else "AI Email Assistant")
st.caption(
    "Read, summarize and reply to your Gmail and WhatsApp inquiries. Nothing is sent without your approval."
    if WA_ON
    else "Read, summarize and reply to your Gmail. Nothing is sent without your approval."
)
# ---------- session state ----------
defaults = {
    "messages": [],
    "thread_id": str(uuid.uuid4()),  # separate memory per browser session
    "pending": None,                 # drafts waiting for approval
    "draft_id": 0,                   # makes form widgets fresh for every new draft
    "inbox": [],
    "inbox_error": None,
    "originals": {},                 # cache of original emails shown next to reply drafts
}
for key, value in defaults.items():
    st.session_state.setdefault(key, value)
if "agent" not in st.session_state:
    st.session_state.agent = get_agent()

CONFIG = {"configurable": {"thread_id": st.session_state.thread_id}}
pending = st.session_state.pending


def handle_result(res):
    """Store drafts that need approval, or show the agent's reply."""
    interrupts = res.get("__interrupt__")
    if interrupts:
        st.session_state.pending = interrupts[0].value["action_requests"]
        st.session_state.draft_id += 1
        return
    st.session_state.pending = None
    msgs = res["messages"]
    text = msgs[-1].content or "(no reply)"
    # only look at messages after the user's latest request, so old sends never reappear
    last_human = max((i for i, m in enumerate(msgs) if getattr(m, "type", "") == "human"), default=-1)
    tool_lines = [
        m.content
        for m in msgs[last_human + 1:]
        if getattr(m, "type", "") == "tool"
        and isinstance(m.content, str)
        and m.content.startswith(("SENT:", "NOT SENT:"))
        and m.content not in text  # skip it if the model already said it
    ]
    if tool_lines:
        text = "\n\n".join(tool_lines) + "\n\n" + text
    st.session_state.messages.append({"role": "assistant", "content": text})


def resume(decisions):
    res = st.session_state.agent.invoke(Command(resume={"decisions": decisions}), CONFIG)
    handle_result(res)


def get_original(uid):
    uid = str(uid)
    if uid not in st.session_state.originals:
        try:
            st.session_state.originals[uid] = gmail_imap.get_email(uid)
        except gmail_imap.MailError:
            st.session_state.originals[uid] = None
    return st.session_state.originals[uid]


def queue(text):
    st.session_state.queued = text


def md_safe(text):
    """WhatsApp names are typed by strangers: show them as plain text inside Markdown."""
    return re.sub(r"([\\`*_\[\]<>#|~])", r"\\\1", text or "")


def wa_preview(wa_id, limit=8):
    lines = [f"{'Them' if m['direction'] == 'in' else 'You'}: {m['text']}" for m in wa_store.get_messages(wa_id, limit)]
    return "\n".join(lines) or "(no messages)"


@st.fragment(run_every=5)  # only this panel refreshes, so new messages appear on their own
def whatsapp_panel():
    locked = bool(st.session_state.pending)
    st.header("WhatsApp")
    try:
        chats = wa_store.list_chats(limit=10)
    except Exception as e:
        st.error(f"Could not read WhatsApp chats ({type(e).__name__}).")
        return
    if not chats:
        st.caption("No WhatsApp messages yet.")
        return
    for c in chats:
        st.markdown(f"{'🟢 ' if c['unread'] else ''}**{md_safe((c['name'] or '+' + c['wa_id'])[:30])}**")
        st.caption(f"+{c['wa_id']} · reply window {'open' if c['window_open'] else 'closed'}")
        c1, c2 = st.columns(2)
        if c1.button("Summarize", key=f"wa_sum_{c['wa_id']}", disabled=locked):
            queue(f"Read WhatsApp chat {c['wa_id']} and summarize it.")
            st.rerun()  # full rerun so the chat input picks up the queued prompt
        if c2.button("Reply", key=f"wa_rep_{c['wa_id']}", disabled=locked or not c["window_open"]):
            queue(f"Read WhatsApp chat {c['wa_id']} and draft a reply to it.")
            st.rerun()


# ---------- sidebar: inbox panel ----------
with st.sidebar:
    st.header("Inbox")
    if st.button("Refresh inbox", disabled=bool(pending), use_container_width=True):
        try:
            st.session_state.inbox = gmail_imap.list_emails(limit=10)
            st.session_state.inbox_error = None
        except gmail_imap.MailError as e:
            st.session_state.inbox_error = str(e)
    if st.session_state.inbox_error:
        st.error(st.session_state.inbox_error)
    for e in st.session_state.inbox:
        st.markdown(f"{'🔵 ' if e['unread'] else ''}**{e['from_name'][:30]}**")
        st.caption(e["subject"][:70])
        c1, c2 = st.columns(2)
        c1.button("Summarize", key=f"sum_{e['uid']}", disabled=bool(pending),
                  on_click=queue, args=(f"Read email {e['uid']} and summarize it.",))
        c2.button("Reply", key=f"rep_{e['uid']}", disabled=bool(pending),
                  on_click=queue, args=(f"Read email {e['uid']} and draft a reply to it.",))
    if WHATSAPP_ENABLED:
        st.divider()
        whatsapp_panel()

# ---------- chat history ----------
for msg in st.session_state.messages:
    st.chat_message(msg["role"]).markdown(msg["content"])

# ---------- approval card: nothing is sent without this ----------
if pending:
    with st.chat_message("assistant"):
        st.markdown(f"**{len(pending)} draft(s) waiting for your approval.** Edit if you like, then send or cancel.")
        with st.form("approval_form"):
            edited = []
            for i, action in enumerate(pending):
                args = action["args"]
                k = f"{st.session_state.draft_id}_{i}"
                if action["name"] == "reply_to_email":
                    original = get_original(args.get("uid"))
                    if original:
                        st.markdown(f"**Reply to {original['from_name']}** <{original['reply_to']}>")
                        subj = original["subject"]
                        shown = subj if subj.lower().startswith("re:") else f"Re: {subj}"
                        st.caption(f"Subject: {shown}")
                        with st.expander("Show original email"):
                            st.text(original["body"] or "(no readable text)")  # st.text = never rendered as HTML
                    else:
                        st.warning(f"Could not load original email {args.get('uid')}.")
                    body = st.text_area("Your reply", value=args.get("body", ""), height=200, key=f"body_{k}")
                    edited.append({"uid": args.get("uid"), "body": body})
                elif action["name"] == "reply_whatsapp":
                    wa_id = wa_store.clean_wa_id(args.get("chat_id"))
                    chat = wa_store.get_chat(wa_id) if wa_id else None
                    if chat:  # recipient shown from the database, never from the model's text
                        st.markdown(f"**WhatsApp reply to {md_safe(chat['name'] or 'Unknown')}** (+{wa_id})")
                        if not chat["window_open"]:
                            st.error("The 24-hour reply window is closed. This reply will not be sent.")
                        with st.expander("Show recent messages"):
                            st.text(wa_preview(wa_id))
                    else:
                        st.warning(f"Unknown WhatsApp chat {args.get('chat_id')}. This reply will not be sent.")
                    body = st.text_area("Your WhatsApp reply", value=args.get("body", ""), height=150, key=f"body_{k}")
                    edited.append({"chat_id": args.get("chat_id"), "body": body})
                else:
                    st.markdown("**New email**")
                    to = st.text_input("To", value=args.get("to", ""), key=f"to_{k}")
                    subject = st.text_input("Subject", value=args.get("subject", ""), key=f"subj_{k}")
                    body = st.text_area("Body", value=args.get("body", ""), height=200, key=f"body_{k}")
                    edited.append({"to": to, "subject": subject, "body": body})
                st.divider()
            c1, c2 = st.columns(2)
            send = c1.form_submit_button("Send", type="primary")
            cancel = c2.form_submit_button("Cancel")

    if send:
        decisions = []
        for action, new_args in zip(pending, edited):
            if new_args == action["args"]:
                decisions.append({"type": "approve"})
            else:
                decisions.append({"type": "edit", "edited_action": {"name": action["name"], "args": new_args}})
        with st.spinner("Sending..."):
            resume(decisions)
        st.rerun()
    if cancel:
        resume([{"type": "reject", "message": "The user cancelled. Do not send this."}] * len(pending))
        st.rerun()

# ---------- chat input (also receives the sidebar buttons) ----------
query = st.chat_input("Ask anything ....", disabled=bool(pending))
if not query and st.session_state.get("queued") and not pending:
    query = st.session_state.pop("queued")
if query:
    st.session_state.messages.append({"role": "user", "content": query})
    st.chat_message("user").markdown(query)
    try:
        with st.spinner("Working..."):
            res = st.session_state.agent.invoke({"messages": [{"role": "user", "content": query}]}, CONFIG)
        handle_result(res)
    except Exception as e:
        err = str(e)
        if "rate_limit" in err or "413" in err or "429" in err:
            reply = "Groq's free limit was reached. Wait a minute and try again, with fewer emails (for example the top 3)."
        else:
            reply = f"Something went wrong ({type(e).__name__}). Please try again."
        st.session_state.messages.append({"role": "assistant", "content": reply})
    st.rerun()