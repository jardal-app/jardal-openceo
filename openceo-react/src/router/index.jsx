import { createBrowserRouter, Navigate, useParams } from "react-router-dom"
import { lazy, Suspense, useContext } from "react"
import App from "../App"
import Loading from '../components/Loading'
import { PermissionCheck } from '../components/PermissionCheck'
import { StoreContext } from '@/context/store'

// Public
import Login from '../pages/Login'

// Private layout
import Dashboard4 from '../components/Dashboard4'

// Pages
const Dashboard = lazy(() => import('@/pages/Dashboard'))
const PersonalSpace = lazy(() => import('@/pages/PersonalSpace'))
const HumanInbox = lazy(() => import('@/pages/HumanInbox'))
const TicketList = lazy(() => import('@/pages/TicketList'))
const TicketDetail = lazy(() => import('@/pages/TicketDetail'))
const TicketForm = lazy(() => import('@/pages/TicketForm'))
const UserList = lazy(() => import('@/pages/admin/UserList'))
const UserForm = lazy(() => import('@/pages/admin/UserForm'))
const GroupList = lazy(() => import('@/pages/admin/GroupList'))
const GroupForm = lazy(() => import('@/pages/admin/GroupForm'))
const ProjectList = lazy(() => import('@/pages/admin/ProjectList'))
const ProjectForm = lazy(() => import('@/pages/admin/ProjectForm'))

function Page({ Component, permissions }) {
  if (permissions && permissions.length)
    return (
      <PermissionCheck perms={permissions}>
        <Suspense fallback={<Loading />}>
          <Component />
        </Suspense>
      </PermissionCheck>
    )
  else
    return (
      <Suspense fallback={<Loading />}>
        <Component />
      </Suspense>
    )
}

// Redirect legacy /tickets/:id URLs to /dashboard/tickets/:id
function RedirectTicket() {
  const { id } = useParams()
  return <Navigate to={`/dashboard/tickets/${id}`} replace />
}

const publicRoutes = [
  { path: "/login", element: <Login /> },
]

// Auth guard wrapper — reactive via StoreContext
function PrivateGuard({ children }) {
  const store = useContext(StoreContext)

  if (!store.user || !localStorage.getItem('heaven')) {
    return <Navigate to="/login" replace />
  }
  return children
}

const router = createBrowserRouter([
  {
    element: <App />,
    children: [
      ...publicRoutes,
      {
        path: "/dashboard",
        element: <PrivateGuard><Suspense fallback={<Loading />}><Dashboard4 /></Suspense></PrivateGuard>,
        children: [
          { index: true, element: <Navigate to="/dashboard/inicio" replace /> },
          { path: 'inicio', element: <Page Component={Dashboard} permissions={[]} /> },
          { path: 'personal', element: <Page Component={PersonalSpace} permissions={['personal_space']} /> },
          { path: 'tickets/list', element: <Page Component={TicketList} permissions={['read_tickets']} /> },
          { path: 'tickets/create', element: <Page Component={TicketForm} permissions={['write_tickets']} /> },
          { path: 'tickets/update/:id', element: <Page Component={TicketForm} permissions={['write_tickets']} /> },
          { path: 'tickets/:id', element: <Page Component={TicketDetail} permissions={['read_tickets']} /> },
          { path: 'inbox', element: <Page Component={HumanInbox} permissions={[]} /> },
          { path: 'review', element: <Navigate to="/dashboard/inicio" replace /> },

          { path: 'admin/users/list', element: <Page Component={UserList} permissions={['manage_users']} /> },
          { path: 'admin/users/create', element: <Page Component={UserForm} permissions={['manage_users']} /> },
          { path: 'admin/users/update/:id', element: <Page Component={UserForm} permissions={['manage_users']} /> },
          { path: 'admin/groups/list', element: <Page Component={GroupList} permissions={['manage_groups']} /> },
          { path: 'admin/groups/create', element: <Page Component={GroupForm} permissions={['manage_groups']} /> },
          { path: 'admin/groups/update/:id', element: <Page Component={GroupForm} permissions={['manage_groups']} /> },
          { path: 'admin/projects/list', element: <Page Component={ProjectList} permissions={['manage_projects']} /> },
          { path: 'admin/projects/create', element: <Page Component={ProjectForm} permissions={['manage_projects']} /> },
          { path: 'admin/projects/update/:name', element: <Page Component={ProjectForm} permissions={['manage_projects']} /> },
        ]
      },
      // Legacy redirects
      { path: "/tickets/:id", element: <RedirectTicket /> },
      { path: "*", element: <Navigate to="/dashboard" replace /> }
    ],
  },
])

export default router
