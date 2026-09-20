export var navigation = [
  { name: 'Inicio', icon: "HomeIcon", href: '/dashboard/inicio', permissions: ['admin_dashboard'] },
  { name: 'Espacio Personal', icon: "SparklesIcon", href: '/dashboard/personal', permissions: ['personal_space'] },
  {
    name: 'Gestión',
    icon: "BriefcaseIcon",
    permissions: [],
    children: [
      { name: 'Tickets', href: '/dashboard/tickets/list', icon: "TicketIcon", permissions: ['read_tickets'] },

      { name: 'Revisión Cliente', href: '/dashboard/review', icon: "ClipboardDocumentCheckIcon", permissions: ['client_review'] },
    ],
  },
]

export var adminNavigation = [
  {
    name: 'Administración',
    icon: "Cog6ToothIcon",
    permissions: [],
    children: [
      { name: 'Usuarios', href: '/dashboard/admin/users/list', icon: "UsersIcon", permissions: ["manage_users"] },
      { name: 'Grupos', href: '/dashboard/admin/groups/list', icon: "UserGroupIcon", permissions: ["manage_groups"] },
      { name: 'Proyectos', href: '/dashboard/admin/projects/list', icon: "FolderIcon", permissions: ["manage_projects"] },
    ],
  },
]

navigation.forEach(element => {
  if (element.children) {
    var permissions = []
    element.children.forEach(element2 => {
      permissions.push(element2.permissions)
    })
    element.permissions = permissions
  }
})

adminNavigation.forEach(element => {
  if (element.children) {
    var permissions = []
    element.children.forEach(element2 => {
      permissions.push(element2.permissions)
    })
    element.permissions = permissions
  }
})
