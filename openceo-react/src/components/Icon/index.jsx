import * as SolidIcons from '@heroicons/react/20/solid'
import * as OutlineIcons from '@heroicons/react/24/outline'

export default function Icon(props) {
  if (props && props.name) {
    const { name, outline = false, ...rest } = props
    const Icon = outline ? OutlineIcons[name] : SolidIcons[name]
    if (Icon) return <Icon {...rest} />
    return "❓"
  } else {
    return "Falta Nombre del Icono"
  }
}
