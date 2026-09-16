import {
  Component,
  Suspense,
  lazy,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { wikiOpenStart } from "./wikiPerformance";
import {
  Bell,
  BookOpen,
  ChartBar,
  Cpu,
  ChatCircleDots,
  CheckSquare,
  FileText,
  Flag,
  FolderSimple,
  GearSix,
  House,
  List,
  MagnifyingGlass,
  SignOut,
  Trash,
  UserCircle,
  X,
  type Icon,
} from "@phosphor-icons/react";
import { ApiError, allPages, clearSession, get, post, setSession } from "./api";
import type { DemoUser, Me, Page, Resource, SearchHit, Version } from "./types";
import {
  AppContext,
  Empty,
  ErrorBox,
  Loading,
  Modal,
  Motion,
  readable,
  useLoad,
  useTask,
  type Navigation,
} from "./ui";
const ResourcesPage = lazy(() => import("./ResourcesPage").then(m => ({default:m.ResourcesPage})));
const CapabilitiesPage = lazy(() => import("./CapabilitiesPage").then(m => ({default:m.CapabilitiesPage})));
import { AmbientEffects } from "./AmbientEffects";
import { LibraryCreate } from "./LibraryManager";
const KnowledgeWorkspace = lazy(() => import("./KnowledgeWorkspace").then(m => ({default:m.KnowledgeWorkspace})));
const ModelsPage = lazy(() => import("./ModelsPage").then(m => ({default:m.ModelsPage})));
const ResourceDetail = lazy(() =>
  import("./ResourceDetail").then((m) => ({ default: m.ResourceDetail })),
);
const ConsultationPage = lazy(() =>
  import("./ConsultationPage").then((m) => ({ default: m.ConsultationPage })),
);
const TasksPage = lazy(() =>
  import("./OperationsPages").then((m) => ({ default: m.TasksPage })),
);
const CasesPage = lazy(() =>
  import("./OperationsPages").then((m) => ({ default: m.CasesPage })),
);
const SettingsPage = lazy(() =>
  import("./OperationsPages").then((m) => ({ default: m.SettingsPage })),
);
const nav: [Navigation, string, Icon][] = [
  ["documents", "文档中心", FolderSimple],
  ["knowledge", "知识空间", BookOpen],
  ["assistant", "智能答疑", ChatCircleDots],
  ["templates", "Agent能力", FileText],
  ["tasks", "审核与任务", CheckSquare],
  ["cases", "问题反馈", Flag],
  ["trash", "回收站", Trash],
];
const route = (): Navigation => {
  const key = location.hash.replace("#/", "").split("?")[0];
  return [...nav.map(([id]) => id), "settings", "models"].includes(key)
    ? (key as Navigation)
    : "documents";
};
function Brand() {
  return (
    <div className="brand">
      <ChartBar size={33} weight="fill" />
      <div>
        <strong>基金运营知识平台</strong>
        <span>FUND OPERATIONS</span>
      </div>
    </div>
  );
}
function Login({
  ready,
  error,
  reload,
}: {
  ready: (me: Me) => void;
  error?: Error;
  reload: () => void;
}) {
  const demos = useLoad(
    (signal) =>
      import.meta.env.DEV
        ? get<DemoUser[]>("/auth/demo", signal)
        : Promise.resolve(null),
    [],
  );
  const task = useTask();
  return (
    <div className="login-screen">
      <Motion className="login-panel">
        <Brand />
        <h1>让知识成为业务的依据</h1>
        <p>登录工作空间，管理资料、复核知识并追溯每个答案。</p>
        {import.meta.env.DEV && demos.data ? (
          <>
            <div className="login-demo-label">本地开发空间 · 演示身份</div>
            <div className="demo-users">
              {demos.data.map((user) => (
                <button
                  key={user.id}
                  disabled={task.busy}
                  onClick={() =>
                    void task.run(async () => {
                      clearSession();
                      await post("/auth/demo", { user_id: user.id });
                      const me = await get<Me>("/me");
                      setSession(me);
                      ready(me);
                    })
                  }
                >
                  <UserCircle size={29} weight="light" />
                  <span>
                    <strong>{user.display_name}</strong>
                    <small>
                      {user.roles
                        ?.map(
                          (r) =>
                            ({
                              admin: "管理员",
                              editor: "编辑者",
                              reviewer: "复核者",
                              reader: "只读",
                              publisher: "发布者",
                            })[r] ?? r,
                        )
                        .join(" / ")}
                    </small>
                  </span>
                </button>
              ))}
            </div>
            <small className="muted">
              演示登录仅在开发模式可用，不用于生产身份验证。
            </small>
          </>
        ) : (
          <a className="button primary" href="/api/v1/auth/login">
            通过机构账号登录
          </a>
        )}
        <ErrorBox
          error={
            task.error ??
            (error instanceof ApiError && error.status === 401
              ? undefined
              : error) ??
            (demos.error instanceof ApiError && demos.error.status === 404
              ? undefined
              : demos.error)
          }
          retry={reload}
        />
        {demos.loading && <Loading label="正在读取登录方式…" />}
      </Motion>
    </div>
  );
}
class ErrorBoundary extends Component<
  { children: ReactNode },
  { error?: Error }
> {
  state: { error?: Error } = {};
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    return this.state.error ? (
      <div className="standard-page">
        <ErrorBox error={this.state.error} />
        <button onClick={() => location.reload()}>重新载入页面</button>
      </div>
    ) : (
      this.props.children
    );
  }
}
export function App() {
  return (
    <ErrorBoundary>
      <Application />
    </ErrorBoundary>
  );
}
function Application() {
  const visualRoot = useRef<HTMLDivElement>(null);
  const [me, setMe] = useState<Me>();
  const [sessionError, setSessionError] = useState<Error>();
  const [starting, setStarting] = useState(true);
  const [page, setPage] = useState<Navigation>(route);
  const [spaceId, setSpaceId] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [toast, setToast] = useState("");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [mobileViewport, setMobileViewport] = useState(
    () => window.matchMedia("(max-width: 820px)").matches,
  );
  useEffect(() => {
    const viewport = window.matchMedia("(max-width: 820px)");
    const updateViewport = () => setMobileViewport(viewport.matches);
    viewport.addEventListener("change", updateViewport);
    return () => viewport.removeEventListener("change", updateViewport);
  }, []);
  const [profile, setProfile] = useState(false);
  const [createFirstLibrary, setCreateFirstLibrary] = useState(false);
  const [detail, setDetail] = useState<{
    resource: Resource;
    tab?: string;
    versionId?: string;
    blockId?: string;
    key: number;
  }>();
  const [askResource, setAskResource] = useState<Resource>();
  const [consultationKey, setConsultationKey] = useState(0);
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [hits, setHits] = useState<SearchHit[]>();
  const [searchCursor, setSearchCursor] = useState<string | null>(null);
  const searchGeneration = useRef(0);
  const searchScope = useRef("");
  searchScope.current = `${me?.id ?? ""}:${spaceId}`;
  const task = useTask();
  const searchTask = useTask();
  const toastTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  const detailKey = useRef(0);
  const navigationGuard = useRef<(()=>boolean)|null>(null);
  const loadMe = () => {
    setStarting(true);
    get<Me>("/me")
      .then((value) => {
        setMe(value);
        setSession(value);
        setSessionError(undefined);
        setSpaceId((id) =>
          value.spaces.some((s) => s.id === id)
            ? id
            : (value.spaces[0]?.id ?? ""),
        );
      })
      .catch((error) => {
        setMe(undefined);
        setSessionError(error);
      })
      .finally(() => setStarting(false));
  };
  useEffect(() => {
    document.documentElement.lang = "zh-CN";
    document.title = "文档中心 · 基金运营知识平台";
    loadMe();
    const expired = () => {
      clearSession();
      setMe(undefined);
      setSessionError(
        new ApiError(401, "SESSION_EXPIRED", "会话已过期，请重新登录。"),
      );
      setDetail(undefined);
      setHits(undefined);
      setSearchOpen(false);
      setAskResource(undefined);
      searchGeneration.current += 1;
    };
    const onHash = () => setPage(route());
    window.addEventListener("session-expired", expired);
    window.addEventListener("hashchange", onHash);
    return () => {
      window.removeEventListener("session-expired", expired);
      window.removeEventListener("hashchange", onHash);
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);
  useEffect(() => {
    if (!me) return;
    const controller = new AbortController();
    get<Me>("/me", controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setMe(value);
          setSession(value);
          setSpaceId((id) =>
            value.spaces.some((s) => s.id === id)
              ? id
              : (value.spaces[0]?.id ?? ""),
          );
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) setSessionError(error);
      });
    return () => controller.abort();
  }, [refresh]);
  useEffect(() => {
    const title = nav.find(([key]) => key === page)?.[1] ?? (page === "models" ? "我的模型" : "设置");
    document.title = `${title} · 基金运营知识平台`;
    setMobileOpen(false);
  }, [page]);
  const notify = (message: string) => {
    setToast(message);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 6500);
  };
  const navigate = (next: Navigation) => {
    if(next === "knowledge") wikiOpenStart();
    location.hash = `/${next}`;
    setPage(next);
  };
  const openResource = (
    resource: Resource,
    tab = resource.kind === "document" ? "preview" : "content",
    versionId?: string,
    blockId?: string,
  ) => {
    if(navigationGuard.current && !navigationGuard.current())return;
    setDetail({ resource, tab, versionId, blockId, key: ++detailKey.current });
  };
  const openVersion = (versionId: string, blockId?: string) => {
    void task.run(async () => {
      const v = await get<Version>(`/versions/${versionId}`);
      const r = await get<Resource>(`/resources/${v.resource_id}`);
      openResource(
        r,
        blockId ? "content" : r.kind === "document" ? "preview" : "content",
        versionId,
        blockId,
      );
    });
  };
  const ask = (resource?: Resource) => {
    setAskResource(resource);
    setConsultationKey((k) => k + 1);
    setDetail(undefined);
    navigate("assistant");
  };
  const space = me?.spaces.find((s) => s.id === spaceId) ?? me?.spaces[0];
  const runSearch = (more = false) =>
    searchTask.run(async () => {
      if (!space) return;
      const generation = ++searchGeneration.current;
      const scope = searchScope.current;
      const result = await post<Page<SearchHit>>("/search", {
        space_id: space.id,
        query: search,
        limit: 20,
        ...(more && searchCursor ? { cursor: searchCursor } : {}),
      });
      if (generation !== searchGeneration.current || scope !== searchScope.current) return;
      setHits((previous) =>
        more ? [...(previous ?? []), ...result.items] : result.items,
      );
      setSearchCursor(result.next_cursor);
    });
  if (starting && !me)
    return (
      <div className="app-start">
        <Brand />
        <Loading label="正在连接工作空间…" />
      </div>
    );
  if (!me)
    return (
      <Login
        ready={(value) => {
          setMe(value);
          setSpaceId(value.spaces[0]?.id ?? "");
          setDetail(undefined);
        }}
        error={sessionError}
        reload={loadMe}
      />
    );
  if (!space)
    return (
      <div className="app-start">
        <Brand />
        <Empty
          title="创建你的第一个知识库"
          detail="个人知识仅自己可见；团队知识库可以邀请同事共同维护。"
        >
          <button className="primary" onClick={() => setCreateFirstLibrary(true)}>创建知识库</button>
          <button onClick={loadMe}>刷新权限</button>
          <button
            onClick={() =>
              void task.run(async () => {
                await post("/auth/logout");
                clearSession();
                setMe(undefined);
              })
            }
          >
            退出登录
          </button>
        </Empty>
        <ErrorBox error={task.error} />
        {createFirstLibrary && <LibraryCreate close={() => setCreateFirstLibrary(false)} created={library => { setSpaceId(library.id); setCreateFirstLibrary(false); loadMe(); }} />}
      </div>
    );
  return (
    <AppContext.Provider
      value={{
        me,
        space,
        refresh,
        bump: () => setRefresh((x) => x + 1),
        navigate,
        notify,
        openResource,
        openVersion,
        ask,
        selectSpace: (id) => {
          if (navigationGuard.current && !navigationGuard.current()) return;
          setSpaceId(id);
          searchGeneration.current += 1;
          setDetail(undefined);
          setAskResource(undefined);
          setHits(undefined);
          setSearchOpen(false);
          setSearch("");
          setSearchCursor(null);
          setRefresh(x => x + 1);
        },
        setNavigationGuard: guard=>{navigationGuard.current=guard;},
        openWikiTitle: async title=>{
          const resolved=await get<{resource_id:string;version_id:string}>(`/wiki/resolve?${new URLSearchParams({space_id:space.id,title:title.split("#")[0]})}`);
          const resource=await get<Resource>(`/resources/${resolved.resource_id}`);
          openResource(resource,"content",resolved.version_id);
        },
      }}
    >
      <div className="app-shell" ref={visualRoot} data-ambient-shell="true" data-page={page}>
        {page !== "documents" && <AmbientEffects rootRef={visualRoot} routeKey={`${page}:${space.id}`} />}
        <a className="skip-link" href="#main-content">
          跳转到主要内容
        </a>
        {mobileOpen && (
          <button
            className="sidebar-backdrop"
            aria-label="关闭侧边导航"
            onClick={() => setMobileOpen(false)}
          />
        )}
        <aside
          id="workspace-navigation"
          inert={mobileViewport && !mobileOpen}
          className={`sidebar ${mobileOpen ? "mobile-open" : ""}`}
        >
          <Brand />
          <span className="sidebar-caption">工作空间</span>
          <nav className="main-nav" aria-label="主导航">
            {nav.map(([key, label, Icon], i) => (
              <button
                key={key}
                className={`${page === key ? "active" : ""} ${i === 4 ? "nav-divider" : ""}`}
                onClick={() => navigate(key)}
                aria-current={page === key ? "page" : undefined}
              >
                <Icon size={22} weight="regular" />
                <span>{label}</span>
              </button>
            ))}
          </nav>
          <div className="sidebar-bottom">
            <span className="sidebar-caption">管理</span>
            <button className={`settings-link ${page === "models" ? "active" : ""}`} onClick={()=>navigate("models")}><Cpu size={23}/>我的模型</button>
            <button
              className={`settings-link ${page === "settings" ? "active" : ""}`}
              onClick={() => navigate("settings")}
            >
              <GearSix size={23} />
              设置
            </button>
            <div className="profile-container">
              <button
                className="profile-button"
                aria-expanded={profile}
                onClick={() => setProfile((v) => !v)}
              >
                <span className="avatar">{me.display_name.slice(0, 1)}</span>
                <span>{me.display_name}</span>
                <UserCircle size={17} />
              </button>
              {profile && (
                <div className="profile-menu">
                  <strong>{me.display_name}</strong>
                  <small>当前会话已登录</small>
                  <button
                    onClick={() =>
                      void task.run(async () => {
                        await post("/auth/logout");
                        clearSession();
                        setMe(undefined);
                        setProfile(false);
                        setDetail(undefined);
                        setHits(undefined);
                        setSearchOpen(false);
                        setAskResource(undefined);
                        searchGeneration.current += 1;
                      })
                    }
                  >
                    <SignOut />
                    退出 / 切换账号
                  </button>
                </div>
              )}
            </div>
          </div>
        </aside>
        <div className="main-shell">
          <header className="topbar">
            <div className="breadcrumbs">
              <button
                className="icon-button mobile-toggle"
                aria-label="打开主导航"
                onClick={() => setMobileOpen(true)}
              >
                <List size={22} />
              </button>
              <button
                aria-label="返回文档中心"
                className="icon-button"
                onClick={() => navigate("documents")}
              >
                <House size={19} />
              </button>
              <span>工作空间</span>
              <span className="breadcrumb-slash">/</span>
              <select
                aria-label="选择工作空间"
                value={space.id}
                onChange={(e) => {
                  if(navigationGuard.current && !navigationGuard.current()) return;
                  setSpaceId(e.target.value);
                  searchGeneration.current += 1;
                  setDetail(undefined);
                  setAskResource(undefined);
                  setHits(undefined);
                  setSearchOpen(false);
                  setSearch("");
                  setSearchCursor(null);
                }}
              >
                {me.spaces.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.kind === "personal" ? "个人 · " : s.kind === "team" ? "团队 · " : ""}{s.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="topbar-right">
              <button
                className="global-search"
                aria-label="全局搜索"
                onClick={() => setSearchOpen(true)}
              >
                <MagnifyingGlass size={19} />
                <span>搜索文档、知识、问题…</span>
                <kbd>搜索</kbd>
              </button>
              <button
                className="icon-button"
                aria-label="查看审核与任务"
                title="查看审核与任务"
                onClick={() => navigate("tasks")}
              >
                <Bell size={22} />
              </button>
            </div>
          </header>
          <main id="main-content" className="main-content">
            <ErrorBox error={task.error} />
            <Motion
              key={`${page}:${space.id}`}
              identity={page}
              className="page-root"
            >
              <Suspense fallback={<Loading label="正在加载工作台…" />}>
                {page === "documents" && <ResourcesPage kind="document" />}
                {page === "knowledge" && <KnowledgeWorkspace />}
                {page === "templates" && <CapabilitiesPage />}
                {page === "trash" && <ResourcesPage kind="document" trash />}
                {page === "assistant" && (
                  <ConsultationPage
                    key={consultationKey}
                    initialResource={askResource}
                  />
                )}
                {page === "tasks" && <TasksPage />}
                {page === "cases" && <CasesPage />}
                {page === "settings" && <SettingsPage />}
                {page === "models" && <ModelsPage />}
              </Suspense>
            </Motion>
          </main>
        </div>
      </div>
      {detail && (
        <Suspense
          fallback={
            <div className="overlay-loading">
              <Loading />
            </div>
          }
        >
          <ResourceDetail
            key={detail.key}
            resource={detail.resource}
            initialTab={detail.tab}
            versionId={detail.versionId}
            blockId={detail.blockId}
            close={() => setDetail(undefined)}
          />
        </Suspense>
      )}
      {toast && (
        <div className="toast" role="status">
          {toast}
          <button aria-label="关闭通知" onClick={() => setToast("")}>
            <X size={17} />
          </button>
        </div>
      )}
      {searchOpen && (
        <Modal title="搜索工作空间" close={() => setSearchOpen(false)}>
          <div className="modal-body">
            <form
              className="search-form"
              onSubmit={(e) => {
                e.preventDefault();
                void runSearch();
              }}
            >
              <input
                required
                autoFocus
                aria-label="全局搜索关键词"
                placeholder="搜索标题、条款或知识内容"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <button className="primary" disabled={searchTask.busy}>
                搜索
              </button>
            </form>
            <ErrorBox error={searchTask.error} />
            {searchTask.busy && <Loading />}
            {hits?.map((hit, i) => (
              <button
                className="search-hit"
                key={`${hit.version_id}-${hit.block_id}-${i}`}
                onClick={() => {
                  setSearchOpen(false);
                  openVersion(hit.version_id, hit.block_id);
                }}
              >
                <strong>{hit.title}</strong>
                <p>{hit.excerpt}</p>
                <small>{readable(hit.locator.label)}</small>
              </button>
            ))}
            {hits?.length === 0 && (
              <Empty
                title="未找到可访问的结果"
                detail="尝试换一个业务关键词或条款编号。"
              />
            )}
            {searchCursor && (
              <button
                disabled={searchTask.busy}
                onClick={() => void runSearch(true)}
              >
                加载更多
              </button>
            )}
          </div>
        </Modal>
      )}
    </AppContext.Provider>
  );
}
