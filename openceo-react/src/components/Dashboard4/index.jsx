import { Fragment, useContext, useEffect, useRef, useState } from 'react';
import { Menu, Transition } from '@headlessui/react';
import { Link, Outlet } from 'react-router-dom';
import { createPortal } from 'react-dom';
import { StoreContext } from '@/context/store';
import client from '@/api';
import {
    BellIcon,
    Bars3Icon,
    ChevronDownIcon,
    XMarkIcon,
} from '@heroicons/react/20/solid';
import './Dashboard4.css';
import { navigation, adminNavigation } from "@/router/Menu";
import SideMenu from '../SideMenu';

function classNames(...classes) {
    return classes.filter(Boolean).join(' ')
}

function resolveUserDisplayName(user) {
    const name = String(user?.name || '').trim();
    if (name) return name;
    const username = String(user?.username || '').trim();
    if (username) return username;
    const displayName = String(user?.display_name || '').trim();
    if (displayName) return displayName;
    return 'Account';
}

export default function Dashboard4() {
    const store = useContext(StoreContext);
    const menuButtonRef = useRef(null);
    const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 });
    const menuWidth = 208;
    const menuOffset = 8;
    const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
    const currentUser = store?.user || {};
    const userDisplayName = resolveUserDisplayName(currentUser);
    const userInitial = userDisplayName.charAt(0).toUpperCase() || 'A';
    const toasts = store?.toasts || [];
    const [toastVisible, setToastVisible] = useState(false);
    const [projects, setProjects] = useState([]);

    useEffect(() => {
      client.get('/api/projects').then((res) => {
        const data = res.data || []
        setProjects(data)
        const stored = localStorage.getItem('selectedProject')
        if (!store.selectedProject && data.length > 0) {
          if (stored && data.find((p) => p.name === stored)) {
            store.setProject(stored)
          }
        }
      }).catch(() => {})
    }, [])

    useEffect(() => {
        if (toasts.length > 0) {
            setToastVisible(true);
            const timer = setTimeout(() => setToastVisible(false), toasts[toasts.length - 1]?.duration || 4000);
            return () => clearTimeout(timer);
        } else {
            setToastVisible(false);
        }
    }, [toasts]);

    const updateMenuPosition = () => {
        const rect = menuButtonRef.current?.getBoundingClientRect();
        if (!rect) return;
        const viewportWidth = window.innerWidth || 0;
        const left = Math.min(Math.max(rect.right - menuWidth, 12), Math.max(viewportWidth - menuWidth - 12, 12));
        const top = rect.bottom + menuOffset;
        setMenuPosition({ top, left });
    };


    return (
        <>
            <div className="dashboard4-shell min-h-screen flex overflow-hidden max-w-screen mx-auto bg-black text-slate-100">
                <div className="no-print">
                    <SideMenu navigation={navigation} adminNavigation={adminNavigation} store={store} mobileOpen={mobileMenuOpen} setMobileOpen={setMobileMenuOpen} />
                </div>
                <div className="dashboard4-content flex flex-1 flex-col overflow-hidden">
                    <div className="dashboard4-topbar no-print border-b border-oceo-blue-400/20 bg-black/70 backdrop-blur pb-0">
                        <div className="px-3 sm:px-6 2xl:px-10">

                            <div className="relative flex h-16 items-center justify-between border-b border-oceo-blue-400/10">
                                <div className="flex items-center gap-2 lg:hidden">
                                    <button
                                        type="button"
                                        className="rounded-md border border-oceo-blue-400/30 bg-black/60 p-2 text-slate-200 transition hover:border-oceo-blue-400/60 hover:text-slate-100 focus:outline-none focus:ring-2 focus:ring-oceo-blue-400/40 focus:ring-offset-0"
                                        onClick={() => setMobileMenuOpen(true)}
                                    >
                                        <Bars3Icon className="h-5 w-5 text-oceo-blue-400" aria-hidden="true" />
                                    </button>
                                </div>
                                <div className="w-full flex items-center justify-end">
                                    <div className="w-full flex justify-end items-center gap-3">
                                         {/* Brand + Project Selector */}
                                         <div className="hidden lg:flex items-center gap-3 mr-auto">
                                             <span className="text-oceo-blue-400 font-bold text-sm tracking-wider" style={{ fontFamily: 'var(--font-brand)' }}>OpenCEO</span>
                                             {projects.length > 0 && (
                                               <select
                                                 value={store.selectedProject || ''}
                                                 onChange={(e) => store.setProject(e.target.value)}
                                                 className="input-oceo input-sm bg-black/60 border-oceo-blue-400/30 text-slate-200"
                                                 style={{ width: 190 }}
                                               >
                                                 <option value="">Todos los proyectos</option>
                                                 {projects.map((p) => (
                                                   <option key={p.name} value={p.name}>{p.display_name || p.name}</option>
                                                 ))}
                                               </select>
                                             )}
                                         </div>
                                        <div className="relative">
                                            <button
                                                type="button"
                                                className="rounded-full border border-oceo-blue-400/30 bg-black/60 p-1.5 text-slate-200 transition hover:border-oceo-blue-400/60 hover:text-slate-100 focus:outline-none focus:ring-2 focus:ring-oceo-blue-400/40 focus:ring-offset-0"
                                            >
                                                <span className="sr-only">Ver notificaciones</span>
                                                <BellIcon className="h-4 w-4 sm:h-5 sm:w-5 2xl:h-6 2xl:w-6 text-oceo-blue-400" aria-hidden="true" />
                                            </button>
                                            {toastVisible && toasts.length > 0 && (
                                                <div className="absolute right-0 top-full mt-2 z-[200]">
                                                    <div className="w-80 rounded-lg border border-oceo-blue-400/30 bg-[#0B0F14]/95 shadow-2xl shadow-black/50 overflow-hidden">
                                                        <div className="p-4">
                                                            <div className="flex items-start gap-3">
                                                                <div className="flex-shrink-0 w-8 h-8 rounded-full bg-oceo-blue-400/20 flex items-center justify-center">
                                                                    <BellIcon className="w-4 h-4 text-oceo-blue-400" />
                                                                </div>
                                                                <div className="flex-1 min-w-0">
                                                                    <p className="text-sm font-medium text-slate-100">
                                                                        {toasts[toasts.length - 1]?.title || 'Notificación'}
                                                                    </p>
                                                                    {toasts[toasts.length - 1]?.message && (
                                                                        <p className="mt-1 text-xs text-slate-400 line-clamp-2">
                                                                            {toasts[toasts.length - 1].message}
                                                                        </p>
                                                                    )}
                                                                </div>
                                                                <button
                                                                    onClick={() => store?.removeToast?.(toasts[toasts.length - 1]?.id)}
                                                                    className="flex-shrink-0 text-slate-500 hover:text-slate-300 transition"
                                                                >
                                                                    <XMarkIcon className="w-4 h-4" />
                                                                </button>
                                                            </div>
                                                        </div>
                                                        <div className="h-0.5 bg-oceo-blue-400/30">
                                                            <div className="h-full bg-oceo-blue-400 animate-[shrink_5s_linear_forwards]" style={{ animationDuration: `${toasts[toasts.length - 1]?.duration || 5000}ms` }} />
                                                        </div>
                                                    </div>
                                                </div>
                                            )}
                                        </div>
                                        {/* Profile dropdown */}
                                        <Menu as="div" className="relative ml-3">
                                            {({ open: menuOpen }) => {
                                                useEffect(() => {
                                                    if (menuOpen) updateMenuPosition();
                                                }, [menuOpen]);
                                                useEffect(() => {
                                                    if (!menuOpen) return;
                                                    const handleResize = () => updateMenuPosition();
                                                    window.addEventListener('resize', handleResize);
                                                    window.addEventListener('scroll', handleResize, true);
                                                    return () => {
                                                        window.removeEventListener('resize', handleResize);
                                                        window.removeEventListener('scroll', handleResize, true);
                                                    };
                                                }, [menuOpen]);
                                                return (
                                                    <>
                                                        <div>
                                                            <Menu.Button ref={menuButtonRef} className="flex max-w-xs items-center gap-2 rounded-full border border-oceo-blue-400/30 bg-black/60 px-2 py-1 text-xs text-slate-200 shadow-sm transition hover:border-oceo-blue-400/60 hover:bg-black/80 focus:outline-none focus:ring-2 focus:ring-oceo-blue-400/40 lg:rounded-xl lg:px-3">
                                                                <span className="dashboard4-avatar inline-flex h-6 w-6 sm:h-6 sm:w-6 2xl:h-8 2xl:w-8 items-center justify-center rounded-full bg-black/70">
                                                                    <span className="text-sm font-medium leading-none text-slate-100">{userInitial}</span>
                                                                </span>
                                                                <span className="2xl:ml-6 ml-4 hidden text-sm font-medium text-slate-200 lg:block">
                                                                    <span className="sr-only">Abrir menú de usuario para </span>{userDisplayName}
                                                                </span>
                                                                <ChevronDownIcon
                                                                    className="ml-1 hidden h-6 w-6 flex-shrink-0 text-slate-400 lg:block"
                                                                    aria-hidden="true"
                                                                />
                                                            </Menu.Button>
                                                        </div>
                                                        {createPortal(
                                                            <Transition
                                                                show={menuOpen}
                                                                as={Fragment}
                                                                enter="transition ease-out duration-100"
                                                                enterFrom="transform opacity-0 scale-95"
                                                                enterTo="transform opacity-100 scale-100"
                                                                leave="transition ease-in duration-75"
                                                                leaveFrom="transform opacity-100 scale-100"
                                                                leaveTo="transform opacity-0 scale-95"
                                                            >
                                                                <Menu.Items
                                                                    className="fixed z-[99999] w-52 origin-top-right rounded-xl border border-oceo-blue-400/20 bg-black/90 py-1 shadow-[0_20px_40px_-30px_rgba(0,0,0,0.85)] backdrop-blur focus:outline-none"
                                                                    style={{ top: menuPosition.top, left: menuPosition.left }}
                                                                >
                                                                    <Menu.Item>
                                                                        {({ active }) => (
                                                                            <Link
                                                                                to="/dashboard/inicio"
                                                                                className={classNames(active ? 'bg-black/70' : '', 'block px-4 py-2 sm:text-sm text-xs text-slate-200 w-full text-left')}
                                                                            >
                                                                                Dashboard
                                                                            </Link>
                                                                        )}
                                                                    </Menu.Item>
                                                                    <Menu.Item>
                                                                        {({ active }) => (
                                                                            <button
                                                                                onClick={() => store.logout()}
                                                                                className={classNames(active ? 'bg-black/70' : '', 'block px-4 py-2 sm:text-sm text-xs text-slate-200 w-full text-left cursor-pointer')}
                                                                            >
                                                                                Cerrar sesión
                                                                            </button>
                                                                        )}
                                                                    </Menu.Item>
                                                                </Menu.Items>
                                                            </Transition>,
                                                            document.body
                                                        )}
                                                    </>
                                                );
                                            }}
                                        </Menu>
                                    </div>
                                </div>
                            </div>

                        </div>
                    </div>
                    <main className="dashboard4-main min-h-0 flex-1 max-w-full overflow-x-hidden overflow-y-auto">
                        <div className='mx-auto mt-4 sm:mt-6 px-2 sm:px-6 2xl:px-10'>
                            <div className="dashboard4-panel relative overflow-x-hidden max-w-full rounded-2xl border border-oceo-blue-400/20 bg-black/60 shadow-[0_30px_60px_-45px_rgba(0,0,0,0.85)] backdrop-blur">
                                <Outlet />
                            </div>
                        </div>

                    </main>

                </div>
            </div>
        </>
    )
}
