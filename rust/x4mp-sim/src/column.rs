//! Type-erased dense columns and component bundles.

use std::any::{Any, TypeId};
use std::fmt;

use crate::comp::Component;
use crate::hash::Fnv64;
use crate::shadow::Comparator;

/// Source of one row when a world is rebuilt (stable merge of kept and staged rows).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Src {
    Old(u32),
    New(u32),
}

pub(crate) trait AnyColumn: Send + Sync {
    fn name(&self) -> &'static str;
    fn len(&self) -> usize;
    fn as_any(&self) -> &dyn Any;
    fn as_any_mut(&mut self) -> &mut dyn Any;
    fn push_default(&mut self);
    /// Pushes a boxed `T`; returns the box back on a type mismatch.
    fn push_boxed(&mut self, v: Box<dyn Any + Send>) -> Result<(), Box<dyn Any + Send>>;
    fn clone_row(&self, row: usize) -> Box<dyn Any + Send>;
    /// Replaces the contents by `plan` (rows taken from self and from `staged`), and empties
    /// `staged`.
    fn rebuild(&mut self, plan: &[Src], staged: &mut dyn AnyColumn);
    /// Applies a permutation: new row i = old row perm[i].
    fn permute(&mut self, perm: &[u32]);
    /// Removes the rows flagged in `dead` in place, keeping the order of the others.
    fn compact(&mut self, dead: &[bool]);
    /// Appends `staged` rows in `order` (moving them out), then empties `staged`.
    fn append_from(&mut self, staged: &mut dyn AnyColumn, order: &[u32]);
    fn clear(&mut self);
    fn hash_into(&self, h: &mut Fnv64);
    fn hash_row(&self, row: usize, h: &mut Fnv64);
    fn compare_row(&self, row: usize, other: &dyn AnyColumn, other_row: usize, c: &mut Comparator);
    fn new_empty(&self) -> Box<dyn AnyColumn>;
    /// Copies all values into `dst` (same type), reusing its allocation.
    fn copy_into(&self, dst: &mut dyn AnyColumn);
    fn debug_row(&self, row: usize) -> String;
}

pub(crate) struct Column<T: Component>(pub Vec<T>);

impl<T: Component> Column<T> {
    pub fn new() -> Column<T> {
        Column(Vec::new())
    }
}

pub(crate) fn new_column<T: Component>() -> Box<dyn AnyColumn> {
    Box::new(Column::<T>::new())
}

impl<T: Component> AnyColumn for Column<T> {
    fn name(&self) -> &'static str {
        T::NAME
    }
    fn len(&self) -> usize {
        self.0.len()
    }
    fn as_any(&self) -> &dyn Any {
        self
    }
    fn as_any_mut(&mut self) -> &mut dyn Any {
        self
    }
    fn push_default(&mut self) {
        self.0.push(T::default());
    }
    fn push_boxed(&mut self, v: Box<dyn Any + Send>) -> Result<(), Box<dyn Any + Send>> {
        let v = v.downcast::<T>()?;
        self.0.push(*v);
        Ok(())
    }
    fn clone_row(&self, row: usize) -> Box<dyn Any + Send> {
        Box::new(self.0[row].clone())
    }
    fn rebuild(&mut self, plan: &[Src], staged: &mut dyn AnyColumn) {
        let staged = &mut staged.as_any_mut().downcast_mut::<Column<T>>().expect("column type mismatch").0;
        let mut out = Vec::with_capacity(plan.len());
        for src in plan {
            out.push(match *src {
                Src::Old(i) => std::mem::take(&mut self.0[i as usize]),
                Src::New(i) => std::mem::take(&mut staged[i as usize]),
            });
        }
        self.0 = out;
        staged.clear();
    }
    fn permute(&mut self, perm: &[u32]) {
        let mut out = Vec::with_capacity(perm.len());
        for &i in perm {
            out.push(std::mem::take(&mut self.0[i as usize]));
        }
        self.0 = out;
    }
    fn compact(&mut self, dead: &[bool]) {
        let mut i = 0usize;
        self.0.retain(|_| {
            let keep = !dead.get(i).copied().unwrap_or(false);
            i += 1;
            keep
        });
    }
    fn append_from(&mut self, staged: &mut dyn AnyColumn, order: &[u32]) {
        let staged = &mut staged.as_any_mut().downcast_mut::<Column<T>>().expect("column type mismatch").0;
        self.0.reserve(order.len());
        for &i in order {
            self.0.push(std::mem::take(&mut staged[i as usize]));
        }
        staged.clear();
    }
    fn clear(&mut self) {
        self.0.clear();
    }
    fn hash_into(&self, h: &mut Fnv64) {
        for v in &self.0 {
            v.stable_hash(h);
        }
    }
    fn hash_row(&self, row: usize, h: &mut Fnv64) {
        self.0[row].stable_hash(h);
    }
    fn compare_row(&self, row: usize, other: &dyn AnyColumn, other_row: usize, c: &mut Comparator) {
        let other = &other.as_any().downcast_ref::<Column<T>>().expect("column type mismatch").0;
        c.field(T::NAME, |c| self.0[row].compare(&other[other_row], c));
    }
    fn new_empty(&self) -> Box<dyn AnyColumn> {
        Box::new(Column::<T>::new())
    }
    fn copy_into(&self, dst: &mut dyn AnyColumn) {
        let dst = &mut dst.as_any_mut().downcast_mut::<Column<T>>().expect("column type mismatch").0;
        dst.clear();
        dst.extend_from_slice(&self.0);
    }
    fn debug_row(&self, row: usize) -> String {
        format!("{:?}", self.0[row])
    }
}

/// A set of component values for one entity (spawn, migration).
#[derive(Default)]
pub struct Bundle {
    pub(crate) items: Vec<(TypeId, &'static str, Box<dyn Any + Send>)>,
}

impl Bundle {
    pub fn new() -> Bundle {
        Bundle::default()
    }

    /// Adds or replaces a component value.
    pub fn with<T: Component>(mut self, v: T) -> Bundle {
        self.set(v);
        self
    }

    pub fn set<T: Component>(&mut self, v: T) {
        let id = TypeId::of::<T>();
        if let Some(slot) = self.items.iter_mut().find(|(t, _, _)| *t == id) {
            slot.2 = Box::new(v);
        } else {
            self.items.push((id, T::NAME, Box::new(v)));
        }
    }

    pub fn get<T: Component>(&self) -> Option<&T> {
        let id = TypeId::of::<T>();
        self.items.iter().find(|(t, _, _)| *t == id).and_then(|(_, _, v)| v.downcast_ref::<T>())
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub(crate) fn take(&mut self, id: TypeId) -> Option<Box<dyn Any + Send>> {
        let pos = self.items.iter().position(|(t, _, _)| *t == id)?;
        Some(self.items.swap_remove(pos).2)
    }
}

impl fmt::Debug for Bundle {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_list().entries(self.items.iter().map(|(_, n, _)| n)).finish()
    }
}
