import React, { useEffect, useState } from 'react';
import { ChevronLeftIcon, ChevronRightIcon } from '@heroicons/react/20/solid';
import "./SideMenu.css";
import { Link } from 'react-router-dom';
import Icon from '../Icon';

// Función classNames de Tailwind
function classNames(...classes) {
    return classes.filter(Boolean).join(' ');
}

// Componente para cada ítem del menú
const MenuItem = ({ item, isExpanded, store }) => {
    const [isOpen, setIsOpen] = useState(false);
    const isChildActive = (children) => {
        return children.some(child => verifyRuta(child));
    };
    function verifyRuta(child) {
        if (child.href[0] != "/") {
            return location.pathname.split('/').slice(0, -1).join('/').includes("/dashboard/" + child.href.split('/').slice(0, -1).join('/'));
        } else {
            return location.pathname == child.href;
        }
    }
    return (
        <div className='ml-3  mb-4 '>
            {!item?.children ?
                <>
                    {/* SIN HIJOS */}
                    {store.checkPermissions(item?.permissions) &&
                        <Link className={`flex items-center cursor-alias text-slate-200 ${(verifyRuta(item) && !isOpen) ? 'rounded-l-full bg-slate-800/70' : ''}`} to={item?.href}>
                            <div
                                className="p-2"
                            >
                                <Icon name={item?.icon} className="h-6 w-6 border-r-2 border-slate-700/80 rounded-full text-slate-200" />
                            </div>
                            <div className="flex-1 min-w-0">
                                {isExpanded &&
                                    <span className="block overflow-hidden whitespace-nowrap flex-1 ml-0.5 min-w-0 pl-2 -my-1 rounded-md degraded-background hover:bg-slate-800/60 transition-all duration-300">
                                        {item?.name}
                                    </span>
                                }
                            </div>
                        </Link>
                    }
                </>
                :
                <>
                    {/* CON HIJOS */}
                    {store.checkPermissionsMenu(item?.permissions) &&
                        <div>
                            <div className={`flex items-center cursor-pointer transform transition duration-150 ease-in-out active:scale-95 text-slate-200 ${(isChildActive(item.children) && !isOpen) ? 'rounded-l-full bg-slate-800/70' : ''}`}>
                                <a
                                    className="p-2"
                                    onClick={() => item?.children && setIsOpen(!isOpen)}
                                >
                                    <Icon name={item?.icon} className="h-6 w-6 text-slate-200" />
                                </a>
                                <div className="flex-1 min-w-0" onClick={() => item?.children && setIsOpen(!isOpen)}>
                                    {isExpanded &&
                                        <>
                                            {isOpen ?
                                                <span className="flex items-center justify-between overflow-hidden whitespace-nowrap text-slate-200">
                                                    {item?.name}
                                                    <Icon name="ChevronDownIcon" className="h-5 mr-5" />
                                                </span> :
                                                <span className="flex items-center justify-between overflow-hidden whitespace-nowrap text-slate-200">
                                                    {item?.name}
                                                    <Icon name="ChevronRightIcon" className="h-5 mr-1" />
                                                </span>
                                            }
                                        </>
                                    }
                                </div>
                            </div>
                            <div
                                className={classNames(
                                    'submenu',
                                    isOpen ? 'max-h-40' : 'max-h-0'
                                )}
                                style={{ transition: "max-height 0.3s ease-in-out" }}
                            >
                                {item?.children && item?.children.map((child) => (
                                    <React.Fragment key={child.href}>
                                        {
                                            store.checkPermissions(child?.permissions) &&
                                            !(store.user?.is_client && child.href === '/dashboard/review') &&
                                            <>
                                                <Link className={"flex items-center p-2 cursor-alias text-slate-200 " + (verifyRuta(child) ? 'rounded-l-full bg-slate-800/70' : '')} to={child.href}>
                                                    <Icon name={child.icon} className="h-5 w-5 mr-2 text-slate-200 border-r-2 border-slate-700/80 rounded-full" />
                                                    <div className="flex-1 min-w-0">
                                                        {isExpanded &&
                                                            <span className="block overflow-hidden whitespace-nowrap flex-1 ml-0.5 min-w-0 pl-2 -my-1 rounded-md degraded-background hover:bg-slate-800/60 transition-all duration-300">
                                                                {child?.name}
                                                            </span>
                                                        }
                                                    </div>
                                                </Link>
                                            </>
                                        }
                                    </React.Fragment>
                                ))}
                            </div>
                        </div>
                    }
                </>
            }
            <div className="border-top-right-sub" />
        </div>
    );
};



const SideMenu = ({ store, navigation, adminNavigation, mobileOpen = false, setMobileOpen }) => {
    const [isExpanded, setIsExpanded] = useState(false);
    const [isFixed, setIsFixed] = useState(false);
    const [initFixed, setInitFixed] = useState(false);
    const [showCollapseHint, setShowCollapseHint] = useState(false);
    const [isMobile, setIsMobile] = useState(false);

    const handleMouseEnter = () => {
        if (isMobile) return;
        if (!isFixed && !initFixed) {
            setIsExpanded(true);
        }
    };

    const handleMouseLeave = () => {
        if (isMobile) return;
        if (!isFixed && !initFixed) {
            setIsExpanded(false);
        }
    };

    const toggleFixedMenu = () => {
        if (isMobile) {
            if (setMobileOpen) setMobileOpen(!mobileOpen);
            return;
        }
        if (!isFixed) {
            setIsExpanded(false);
            setInitFixed(true);
            setTimeout(() => {
                setIsFixed(!isFixed);
                setIsExpanded(!isFixed);
            }, 500);
            setTimeout(() => {
                setInitFixed(false);
            }, 1000);
        } else {
            setInitFixed(true);
            setIsFixed(!isFixed);
            setIsExpanded(!isFixed);
            setTimeout(() => {
                setInitFixed(false);
            }, 1000);
        }
    };

    useEffect(() => {
        let timer;
        if (isExpanded) {
            timer = setTimeout(() => setShowCollapseHint(true), 320);
        } else {
            setShowCollapseHint(false);
        }
        return () => {
            if (timer) clearTimeout(timer);
        };
    }, [isExpanded]);

    useEffect(() => {
        const media = window.matchMedia('(max-width: 1024px)');
        const handler = () => setIsMobile(media.matches);
        handler();
        if (media.addEventListener) {
            media.addEventListener('change', handler);
        } else {
            media.addListener(handler);
        }
        return () => {
            if (media.removeEventListener) {
                media.removeEventListener('change', handler);
            } else {
                media.removeListener(handler);
            }
        };
    }, []);

    const expandedState = isMobile ? mobileOpen : isExpanded;
    const menuClass = isMobile ? "mobile" : (isFixed ? "w-65" : "w-14");
    const collapseVisible = isMobile ? expandedState : showCollapseHint;

    return (
        <>
            {isMobile &&
                <div
                    className={classNames('side-menu-backdrop', expandedState && 'show')}
                    onClick={() => setMobileOpen && setMobileOpen(false)}
                />
            }
            <div className={"no-print side-menu-wrapper " + menuClass + " transition-width duration-300 ease-in-out"}>
            <div
                className={classNames(
                    'side-menu bg-gradient-to-b from-black via-black to-neutral-950 shadow-2xl text-slate-200',
                    expandedState && 'expanded',
                    isMobile && 'mobile',
                    expandedState && isMobile && 'mobile-open'
                )}
                onMouseEnter={handleMouseEnter}
                onMouseLeave={handleMouseLeave}
            >
                <div className="flex flex-col border-b border-oceo-blue-400/20 px-2 py-3 gap-2">
                    <div className="relative flex items-center w-full">
                        <button type="button" onClick={toggleFixedMenu} className="flex items-center shrink-0">
                            <div className={`h-12 w-12 shrink-0 rounded-lg bg-black/70 ring-1 ring-oceo-blue-400/30 shadow-[0_0_18px_rgba(46,134,230,0.18)] flex items-center justify-center transition-transform duration-300 ease-out origin-center transform-gpu ${expandedState ? 'scale-[1.16] translate-x-1.5' : 'scale-100 translate-x-px'}`}>
                                <span className="text-oceo-blue-400 font-bold text-sm" style={{ fontFamily: 'var(--font-brand)' }}>OC</span>
                            </div>
                        </button>
                        {expandedState &&
                            <button
                                type="button"
                                onClick={toggleFixedMenu}
                                className={`absolute right-0 flex h-8 w-8 items-center justify-center rounded-md border border-oceo-blue-400/40 bg-black/70 text-white transition-opacity duration-200 ${collapseVisible ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}
                            >
                                <ChevronLeftIcon className="h-4 w-4 text-white" />
                            </button>
                        }
                    </div>
                    {!expandedState &&
                        <button
                            type="button"
                            onClick={toggleFixedMenu}
                            className="mt-1 self-center flex h-8 w-8 items-center justify-center rounded-md border border-oceo-blue-400/40 bg-black/70 text-white transition"
                        >
                            <ChevronRightIcon className="h-4 w-4" />
                        </button>
                    }
                </div>
                <div className='my-2 '>
                    {navigation.map(item =>
                        <MenuItem key={item.name} item={item} isExpanded={expandedState} store={store} />
                    )}
                </div>
                <div className="pt-2 border-top-right " />
                {(store?.user?.is_admin || true) &&
                    <div>
                        {adminNavigation.map(item =>
                            <MenuItem key={item.name} item={item} isExpanded={expandedState} store={store} />
                        )}
                    </div>
                }
            </div>
            </div>
        </>
    );
};

export default SideMenu;
