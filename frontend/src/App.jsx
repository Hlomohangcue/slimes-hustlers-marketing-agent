import { useCallback, useEffect, useState } from 'react';
import { Activity, ArrowUpRight, Bot, Check, CheckCircle2,
      ChevronRight, CircleAlert, Clock3, LayoutDashboard, 
      LoaderCircle, Plus, RefreshCw, Send, ShieldCheck, Sparkles,
      X, XCircle, Zap } from "lucide-react";
import './App.css';
import { createSessionEpoch, resolveForSession } from './sessionEpoch.js';

const API = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8765';

function App() {
  const [sessionEpoch] = useState(() => createSessionEpoch());
  const [token, setToken] = useState('');
  const [loginValue, setLoginValue] = useState('');
  const [loginError, setLoginError] = useState('');
  const [page, setPage] = useState('Overview');
  const [campaigns, setCampaigns] = useState([]);
  const [posts, setPosts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState('');
  const [form, setForm] = useState({
    name: '', objective: '', audience: '', offer: '',
    call_to_action: 'Contact Slimes.Hustlers to learn more',
  });

  const renderGeneration = sessionEpoch.current();
  const request = useCallback(async (path, options = {}) => {
    const generation = renderGeneration;
    if (!token || !sessionEpoch.isCurrent(generation)) {
      throw new Error('Your session changed. Please sign in again.');
    }
    let response;
    try {
      response = await fetch(`${API}${path}`, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          ...options.headers,
          Authorization: `Bearer ${token}`,
        },
      });
    } catch {
      if (!sessionEpoch.isCurrent(generation)) {
        throw new Error('Your session changed. Please sign in again.');
      }
      throw new Error('Cannot reach the API. Check your connection and API address.');
    }
    if (!sessionEpoch.isCurrent(generation)) {
      throw new Error('Your session changed. Please sign in again.');
    }
    if (response.status === 401 || response.status === 403) {
      sessionEpoch.invalidate();
      setLoginError('Your session expired or was rejected. Please sign in again.');
      setToken('');
      setCampaigns([]);
      setPosts([]);
      setShowForm(false);
      setBusy('');
      setLoading(false);
      throw new Error('Your session expired or was rejected. Please log in again.');
    }
    const data = await response.json().catch(() => ({}));
    if (!sessionEpoch.isCurrent(generation)) {
      throw new Error('Your session changed. Please sign in again.');
    }
    if (!response.ok) {
      throw new Error(data.detail || `Request failed (${response.status})`);
    }
    return data;
  }, [renderGeneration, sessionEpoch, token]);

  const refresh = useCallback(async () => {
    const generation = renderGeneration;
    if (!sessionEpoch.isCurrent(generation)) return;
    setLoading(true);
    setError('');
    try {
      const result = await resolveForSession(sessionEpoch, generation, Promise.all([
        request('/campaigns'),
        request('/posts'),
      ]));
      if (!result.current) return;
      const [campaignData, postData] = result.value;
      setCampaigns(campaignData);
      setPosts(postData);
    } catch (err) {
      if (sessionEpoch.isCurrent(generation)) setError(err.message);
    } finally {
      if (sessionEpoch.isCurrent(generation)) setLoading(false);
    }
  }, [renderGeneration, request, sessionEpoch]);

  
useEffect(() => {
  if (!token) return undefined;
  const generation = sessionEpoch.current();
  let cancelled = false;

  async function loadInitialData() {
    try {
      const result = await resolveForSession(sessionEpoch, generation, Promise.all([
        request('/campaigns'),
        request('/posts'),
      ]));

      if (!cancelled && result.current) {
        const [campaignData, postData] = result.value;
        setCampaigns(campaignData);
        setPosts(postData);
      }
    } catch (err) {
      if (!cancelled && sessionEpoch.isCurrent(generation)) {
        setError(
          err.message
        );
      }
    } finally {
      if (!cancelled && sessionEpoch.isCurrent(generation)) {
        setLoading(false);
      }
    }
  }

  void loadInitialData();

  return () => {
    cancelled = true;
  };
}, [request, sessionEpoch, token]);

  async function login(event) {
    event.preventDefault();
    const generation = sessionEpoch.invalidate();
    setLoginError('');
    try {
      const response = await fetch(`${API}/campaigns`, {
        headers: { Authorization: `Bearer ${loginValue}` },
      });
      if (!sessionEpoch.isCurrent(generation)) return;
      if (response.status === 401 || response.status === 403) {
        setLoginError('That access secret was not accepted.');
        return;
      }
      if (!response.ok) {
        setLoginError(response.status === 503
          ? 'The server authentication is not configured correctly.'
          : `The API returned an error (${response.status}).`);
        return;
      }
      setToken(loginValue);
      setLoginValue('');
      setLoginError('');
      setLoading(true);
    } catch {
      if (sessionEpoch.isCurrent(generation)) {
        setLoginError('Cannot reach the API. Check your connection and API address.');
      }
    }
  }

  function logout() {
    sessionEpoch.invalidate();
    setToken('');
    setCampaigns([]);
    setPosts([]);
    setShowForm(false);
    setBusy('');
    setLoading(false);
    setError('');
    setNotice('');
  }


const pending = posts.filter((post) => post.status === 'PENDING_APPROVAL');
const approved = posts.filter((post) => post.status === 'APPROVED');
const rejected = posts.filter((post) => post.status === 'REJECTED');

const geminiCount = posts.filter(
  (post) => post.generation_source === 'gemini'
).length;

const templateCount = posts.filter(
  (post) => post.generation_source !== 'gemini'
).length;

  async function createCampaign(event) {
    event.preventDefault();
    const generation = renderGeneration;
    if (!sessionEpoch.isCurrent(generation)) return;
    setBusy('create');
    setError('');
    try {
      const campaign = await request('/campaigns', {
        method: 'POST',
        body: JSON.stringify(form),
      });
      if (!sessionEpoch.isCurrent(generation)) return;
      await request(`/campaigns/${campaign.campaign_id}/drafts`, {
        method: 'POST',
      });
      if (!sessionEpoch.isCurrent(generation)) return;
      setNotice('Campaign created. LinkedIn and Facebook drafts are ready for review.');
      setForm({
        name: '', objective: '', audience: '', offer: '',
        call_to_action: 'Contact Slimes.Hustlers to learn more',
      });
      setShowForm(false);
      setPage('Drafts');
      await refresh();
    } catch (err) {
      if (sessionEpoch.isCurrent(generation)) setError(err.message);
    } finally {
      if (sessionEpoch.isCurrent(generation)) setBusy('');
    }
  }

  async function reviewPost(post, action) {
    const generation = renderGeneration;
    if (!sessionEpoch.isCurrent(generation)) return;
    setBusy(post.post_id);
    setError('');
    setNotice('');
    try {
      await request(`/posts/${post.post_id}/${action}`, { method: 'POST' });
      if (!sessionEpoch.isCurrent(generation)) return;
      setNotice(`Post ${action === 'approve' ? 'approved' : 'rejected'} successfully.`);
      await refresh();
    } catch (err) {
      if (sessionEpoch.isCurrent(generation)) setError(err.message);
    } finally {
      if (sessionEpoch.isCurrent(generation)) setBusy('');
    }
  }

  const visiblePosts = page === 'Drafts'
    ? posts
    : pending;

  if (!token) {
    return <main className="login-screen"><form className="campaign-modal login-card" onSubmit={login}>
      <div className="eyebrow"><ShieldCheck size={14} /> PRIVATE WORKSPACE</div>
      <h1>Sign in to your workspace</h1>
      <p>Enter the access secret supplied by your administrator.</p>
      {loginError && <div className="alert error-alert" role="alert">{loginError}</div>}
      <label>Access secret<input type="password" autoComplete="off" required value={loginValue} onChange={(event) => setLoginValue(event.target.value)} /></label>
      <button className="primary-button" type="submit">Sign in</button>
    </form></main>;
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#" onClick={(e) => { e.preventDefault(); setPage('Overview'); }}>
          <span className="brand-mark"><Bot size={25} /></span>
          <span><strong>SLIMES<span className="gold">.</span>HUSTLERS</strong><small>MARKETING INTELLIGENCE</small></span>
        </a>

        <div className="workspace-label">WORKSPACE</div>
        <nav className="navigation">
          <button className={page === 'Overview' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('Overview')}>
            <LayoutDashboard size={18} /> Overview
          </button>
          <button className={page === 'Campaigns' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('Campaigns')}>
            <Activity size={18} /> Campaigns <span className="nav-count">{campaigns.length}</span>
          </button>
          <button className={page === 'Drafts' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('Drafts')}>
            <Send size={18} /> Draft review
            {pending.length > 0 && <span className="nav-count gold-count">{pending.length}</span>}
          </button>
        </nav>

        <div className="sidebar-bottom">
          <div className="ai-card">
            <span className="ai-card-icon"><Sparkles size={17} /></span>
            <strong>Your AI marketing workspace</strong>
            <p>Plan campaigns, prepare content and keep every post under human control.</p>
            <span className="local-tag">
  <span className="status-dot" />
  {geminiCount} Gemini AI · {templateCount} templates tracked
</span>
          </div>
          <div className="user-profile">
            <div className="avatar">SH</div>
            <div><strong>Slimes.Hustlers</strong><small>Workspace administrator</small></div>
            <ChevronRight size={16} className="muted-icon" />
          </div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs">Workspace <ChevronRight size={14} /> <strong>{page}</strong></div>
          <div className="topbar-right">
            <span className="connection"><span className={`status-dot ${error ? 'offline' : ''}`} /> {error ? 'Connection issue' : 'API workspace'}</span>
            <button className="icon-button" title="Refresh data" onClick={refresh}><RefreshCw size={17} /></button>
            <button className="secondary-button" onClick={logout}>Log out</button>
          </div>
        </header>

        <div className="page-content">
          {error && <div className="alert error-alert"><CircleAlert size={18} /><span>{error}</span><button onClick={() => setError('')}><X size={16} /></button></div>}
          {notice && <div className="alert success-alert"><CheckCircle2 size={18} /><span>{notice}</span><button onClick={() => setNotice('')}><X size={16} /></button></div>}

          {page === 'Overview' && <>
            <section className="welcome-row">
              <div><div className="eyebrow"><Sparkles size={14} /> YOUR MARKETING COMMAND CENTRE</div><h1>Good marketing starts <span className="gold">with a plan.</span></h1><p>Build campaigns, review content and keep your brand in control.</p></div>
              <button className="primary-button" onClick={() => setShowForm(true)}><Plus size={18} /> New campaign</button>
            </section>

            <section className="metrics-grid">
              <Metric icon={<Activity />} label="Total campaigns" value={campaigns.length} foot="Campaigns created" />
              <Metric icon={<Clock3 />} label="Awaiting review" value={pending.length} foot="Human approval required" highlight />
              <Metric icon={<CheckCircle2 />} label="Approved posts" value={approved.length} foot="Approved in workspace" />
              <Metric icon={<XCircle />} label="Rejected posts" value={rejected.length} foot="Not approved for publishing" />
            </section>

            <section className="overview-grid">
              <div className="panel campaign-panel">
                <div className="panel-heading"><div><h2>Recent campaigns</h2><p>Your latest marketing initiatives</p></div><button className="text-button" onClick={() => setPage('Campaigns')}>View all <ArrowUpRight size={15} /></button></div>
                {loading ? <Loading /> : campaigns.length === 0 ? <Empty title="No campaigns yet" text="Create your first campaign to get started." /> :
                  <div className="campaign-list">{[...campaigns].reverse().slice(0, 5).map((c) => <CampaignRow key={c.campaign_id} campaign={c} posts={posts} />)}</div>}
              </div>

              <div className="panel approval-panel">
                <div className="panel-heading"><div><h2>Needs your attention</h2><p>Content waiting for your decision</p></div><span className="mini-badge">{pending.length} pending</span></div>
                {loading ? <Loading /> : pending.length === 0 ? <Empty title="You're all caught up" text="New drafts will appear here for review." /> :
                  <div className="attention-list">{pending.slice(0, 4).map((post) => <div className="attention-row" key={post.post_id}><PlatformIcon platform={post.platform} /><div className="attention-copy"><strong>{platformName(post.platform)} draft</strong><small>{post.content.slice(0, 65)}...</small></div><button className="review-link" onClick={() => setPage('Drafts')}>Review <ChevronRight size={14} /></button></div>)}</div>}
              </div>
            </section>

            <section className="human-control-banner"><div className="shield-icon"><ShieldCheck size={24} /></div><div><strong>Human approval is always in control</strong><p>Drafts are not published automatically. Review and approve each post before any future publishing integration.</p></div><span className="banner-pill"><Check size={14} /> Approval gate active</span></section>
          </>}

          {page === 'Campaigns' && <>
            <section className="welcome-row"><div><div className="eyebrow">MARKETING WORKSPACE</div><h1>Your <span className="gold">campaigns.</span></h1><p>Organise initiatives and generate platform-specific drafts.</p></div><button className="primary-button" onClick={() => setShowForm(true)}><Plus size={18} /> New campaign</button></section>
            <section className="panel full-panel"><div className="panel-heading"><div><h2>All campaigns</h2><p>{campaigns.length} campaign(s) in your workspace</p></div></div>
              {loading ? <Loading /> : campaigns.length === 0 ? <Empty title="No campaigns yet" text="Create a campaign to see it listed here." /> : <div className="campaign-list">{[...campaigns].reverse().map((c) => <CampaignRow key={c.campaign_id} campaign={c} posts={posts} />)}</div>}
            </section>
          </>}

          {page === 'Drafts' && <>
            <section className="welcome-row"><div><div className="eyebrow"><ShieldCheck size={14} /> HUMAN REVIEW QUEUE</div><h1>Every post, <span className="gold">your decision.</span></h1><p>Review content before it can be considered for publishing.</p></div><button className="secondary-button" onClick={refresh}><RefreshCw size={16} /> Refresh queue</button></section>
            <div className="filter-row"><span>{visiblePosts.length} post(s)</span><span className="filter-note"><ShieldCheck size={15} /> No automatic publishing</span></div>
            {loading ? <Loading /> : visiblePosts.length === 0 ? <div className="panel"><Empty title="No posts to display" text="Create a campaign to generate LinkedIn and Facebook drafts." /><button className="primary-button centered-button" onClick={() => setShowForm(true)}><Plus size={17} /> Create campaign</button></div> :
              <div className="draft-grid">{[...visiblePosts].reverse().map((post) => <DraftCard key={post.post_id} post={post} busy={busy === post.post_id} onReview={reviewPost} />)}</div>}
          </>}
        </div>
        <footer className="footer"><span>SLIMES.HUSTLERS AI SOLUTIONS</span><span><span className="status-dot" /> Private workspace · Human approval required</span></footer>
      </main>

      {showForm && <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) setShowForm(false); }}>
        <form className="campaign-modal" onSubmit={createCampaign}>
          <div className="modal-heading"><div><div className="eyebrow"><Zap size={14} /> CAMPAIGN BUILDER</div><h2>Create a campaign</h2><p>We'll prepare one draft per platform for your review.</p></div><button type="button" className="icon-button" onClick={() => setShowForm(false)}><X size={18} /></button></div>
          <label>Campaign name<input required minLength={3} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g. AI automation launch" /></label>
          <label>Objective<input required value={form.objective} onChange={(e) => setForm({ ...form, objective: e.target.value })} placeholder="What do you want to achieve?" /></label>
          <label>Target audience<input required value={form.audience} onChange={(e) => setForm({ ...form, audience: e.target.value })} placeholder="Who are you trying to reach?" /></label>
          <label>Offer or service<input value={form.offer} onChange={(e) => setForm({ ...form, offer: e.target.value })} placeholder="What are you promoting?" /></label>
          <label>Call to action<input required value={form.call_to_action} onChange={(e) => setForm({ ...form, call_to_action: e.target.value })} /></label>
          <div className="modal-note"><ShieldCheck size={17} /> Drafts stay in the workspace until you review them.</div>
          <div className="modal-actions"><button type="button" className="secondary-button" onClick={() => setShowForm(false)}>Cancel</button><button className="primary-button" disabled={busy === 'create'}>{busy === 'create' ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />} Create & generate drafts</button></div>
        </form>
      </div>}
    </div>
  );
}

function Metric({ icon, label, value, foot, highlight }) {
  return <div className="metric-card"><div className="metric-top"><span className={highlight ? 'metric-icon highlight-icon' : 'metric-icon'}>{icon}</span><span className="metric-arrow"><ArrowUpRight size={15} /></span></div><div className="metric-value">{value}</div><div className="metric-label">{label}</div><div className="metric-foot">{foot}</div></div>;
}

function PlatformIcon({ platform }) {
  return (
    <span className={`platform-icon ${platform}`}>
      {platform === 'linkedin' ? 'in' : 'f'}
    </span>
  );
}

function platformName(platform) {
  return platform === 'linkedin' ? 'LinkedIn' : 'Facebook';
}

function StatusBadge({ status }) {
  const config = {
    PENDING_APPROVAL: ['pending', 'Pending review'],
    APPROVED: ['approved', 'Approved'],
    REJECTED: ['rejected', 'Rejected'],
  }[status] || ['pending', status];
  return <span className={`status-badge ${config[0]}`}><span />{config[1]}</span>;
}

function CampaignRow({ campaign, posts }) {
  const campaignPosts = posts.filter((p) => p.campaign_id === campaign.campaign_id);
  return <div className="campaign-row"><div className="campaign-symbol"><Activity size={19} /></div><div className="campaign-details"><strong>{campaign.name}</strong><small>{campaign.objective}</small><span>{campaign.campaign_id}</span></div><div className="campaign-platforms">{['linkedin', 'facebook'].map((platform) => <PlatformIcon key={platform} platform={platform} />)}</div><div className="campaign-count"><strong>{campaignPosts.length}</strong><small>drafts</small></div></div>;
}

function DraftCard({ post, busy, onReview }) {
  return (
    <article className="draft-card">
      <div className="draft-card-top">
        <div className="draft-platform">
          <PlatformIcon platform={post.platform} />
          <div>
            <strong>{platformName(post.platform)}</strong>
            <small>Social content draft</small>
          </div>
        </div>
        <StatusBadge status={post.status} />
      </div>

      <div className="generation-row">
        <span
          className={`generation-badge ${
            post.generation_source === 'gemini' ? 'gemini' : 'template'
          }`}
        >
          <Sparkles size={13} />
          {post.generation_source === 'gemini'
            ? 'Gemini AI'
            : 'Template fallback'}
        </span>
      </div>

      <div className="draft-content">{post.content}</div>

      <div className="draft-meta">
        <span>ID: {post.post_id}</span>
        <span>{post.created_at}</span>
      </div>

      {post.status === 'PENDING_APPROVAL' && (
        <div className="draft-actions">
          <button
            className="reject-button"
            disabled={busy}
            onClick={() => onReview(post, 'reject')}
          >
            {busy ? (
              <LoaderCircle size={16} className="spin" />
            ) : (
              <X size={16} />
            )}
            Reject
          </button>

          <button
            className="approve-button"
            disabled={busy}
            onClick={() => onReview(post, 'approve')}
          >
            {busy ? (
              <LoaderCircle size={16} className="spin" />
            ) : (
              <Check size={16} />
            )}
            Approve draft
          </button>
        </div>
      )}

      {post.status !== 'PENDING_APPROVAL' && (
        <div className="reviewed-note">
          <ShieldCheck size={15} />
          Reviewed · {post.status.toLowerCase()}
        </div>
      )}
    </article>
  );
}

function Loading() {
  return <div className="loading-state"><LoaderCircle className="spin" size={22} /> Loading workspace...</div>;
}

function Empty({ title, text }) {
  return <div className="empty-state"><div className="empty-icon"><Sparkles size={21} /></div><strong>{title}</strong><p>{text}</p></div>;
}

export default App;
